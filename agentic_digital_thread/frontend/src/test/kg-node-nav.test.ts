// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the data-nav="kg-node" delegated-anchor contract (KG-contribution
 * "Bridged via" chips): a chip click switches to the Architecture
 * view, activates the Knowledge Graph tab ('neptune'), and calls focusKgNode
 * with the chip's node id (dynamically imported — the architecture view is
 * lazy-loaded, so thread-clicks must not pull cytoscape into the main chat
 * chunk). A false return from focusKgNode leaves the tab open with no
 * selection — the honest fallback never throws. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { focusKgNodeMock } = vi.hoisted(() => ({ focusKgNodeMock: vi.fn(() => true) }));
vi.mock('../views/architecture/neptune-graph', () => ({ focusKgNode: focusKgNodeMock }));

import { createThreadClickHandler } from '../chat/thread-clicks';

type W = {
  switchView?: (v: string) => void;
  switchArchTab?: (t: string) => void;
  closeDataModal?: () => void;
};

function makeHandler() {
  return createThreadClickHandler({ askQuestion: vi.fn(), savePrompt: vi.fn() });
}

function makeChip(nodeId?: string): HTMLAnchorElement {
  const a = document.createElement('a');
  a.href = '#';
  a.dataset.nav = 'kg-node';
  if (nodeId !== undefined) a.dataset.nodeId = nodeId;
  document.body.appendChild(a);
  return a;
}

beforeEach(() => {
  vi.useFakeTimers();
  focusKgNodeMock.mockClear();
  focusKgNodeMock.mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  document.body.replaceChildren();
  delete (window as W).switchView;
  delete (window as W).switchArchTab;
  delete (window as W).closeDataModal;
});

describe('data-nav="kg-node" delegated anchor', () => {
  it('switches view, activates the neptune tab, and focuses the node', async () => {
    const switchView = vi.fn();
    const switchArchTab = vi.fn();
    (window as W).switchView = switchView;
    (window as W).switchArchTab = switchArchTab;

    const chip = makeChip('subj-aaaaaaaaaaaaaaaa');
    const preventDefault = vi.fn();
    makeHandler()({ target: chip, preventDefault });

    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(switchArchTab).not.toHaveBeenCalled(); // deferred behind the view transition

    await vi.runAllTimersAsync(); // tab-switch timeout + dynamic import + focus timeout
    expect(switchArchTab).toHaveBeenCalledWith('neptune');
    expect(focusKgNodeMock).toHaveBeenCalledTimes(1);
    expect(focusKgNodeMock).toHaveBeenCalledWith('subj-aaaaaaaaaaaaaaaa');
  });

  it('a false focusKgNode return is the honest no-selection fallback (no throw)', async () => {
    (window as W).switchView = vi.fn();
    (window as W).switchArchTab = vi.fn();
    focusKgNodeMock.mockReturnValue(false);

    makeHandler()({ target: makeChip('absent-node'), preventDefault: vi.fn() });
    await expect(vi.runAllTimersAsync()).resolves.not.toThrow();
    expect(focusKgNodeMock).toHaveBeenCalledWith('absent-node');
  });

  it('a chip without a node id navigates nowhere (no focus call)', async () => {
    const switchView = vi.fn();
    (window as W).switchView = switchView;

    makeHandler()({ target: makeChip(undefined), preventDefault: vi.fn() });
    await vi.runAllTimersAsync();
    expect(switchView).not.toHaveBeenCalled();
    expect(focusKgNodeMock).not.toHaveBeenCalled();
  });

  it('does not shadow the self-learning anchor contract', async () => {
    const switchView = vi.fn();
    const switchArchTab = vi.fn();
    (window as W).switchView = switchView;
    (window as W).switchArchTab = switchArchTab;

    const a = document.createElement('a');
    a.href = '#';
    a.dataset.nav = 'self-learning';
    document.body.appendChild(a);
    makeHandler()({ target: a, preventDefault: vi.fn() });

    await vi.runAllTimersAsync();
    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(switchArchTab).toHaveBeenCalledWith('proposed');
    expect(focusKgNodeMock).not.toHaveBeenCalled();
  });
});

describe('navKgNode import failure', () => {
  it('a throwing focusKgNode (chunk/module failure) is swallowed — no unhandled rejection', async () => {
    (window as W).switchView = vi.fn();
    (window as W).switchArchTab = vi.fn();
    focusKgNodeMock.mockImplementationOnce(() => {
      throw new Error('chunk load failed');
    });
    const unhandled = vi.fn();
    process.on('unhandledRejection', unhandled);
    try {
      makeHandler()({ target: makeChip('subj-aaaaaaaaaaaaaaaa'), preventDefault: vi.fn() });
      await vi.runAllTimersAsync(); // drives the import().then(...).catch chain
      expect(focusKgNodeMock).toHaveBeenCalledTimes(1);
      await Promise.resolve(); // flush the rejection microtask
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      process.removeListener('unhandledRejection', unhandled);
    }
  });
});
