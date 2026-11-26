// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — SessionMenu split '+ New' button:
 * - the LEFT '+ New' segment starts a new session in ONE click (the action
 *   that was the dropdown's default-highlighted item) without opening the
 *   menu;
 * - the RIGHT caret segment (aria-haspopup=menu) opens today's dropdown,
 *   whose '➕ New Investigation' row and session rows keep working;
 * - keyboard: Enter/ArrowDown on the caret opens the menu. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SessionMenu } from '../chat/SessionMenu';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let showToast: ReturnType<typeof vi.fn<(msg: string) => void>>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  showToast = vi.fn<(msg: string) => void>();
  window.showToast = showToast;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  delete window.showToast;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function mkProps() {
  return {
    onNewSession: vi.fn(),
    onRehydrate: vi.fn().mockResolvedValue(true),
    focusQuestionInput: vi.fn(),
  };
}

async function render(props: ReturnType<typeof mkProps>) {
  await act(async () => {
    root.render(<SessionMenu {...props} />);
  });
}

const newBtn = () => container.querySelector('#session-menu-new-btn') as HTMLButtonElement;
const caretBtn = () => container.querySelector('#session-menu-trigger') as HTMLButtonElement;
const menu = () => container.querySelector('#session-menu') as HTMLElement;

describe('SessionMenu — split button', () => {
  it('renders two real buttons: "+ New" action segment + caret menu opener', async () => {
    await render(mkProps());
    expect(newBtn()).toBeTruthy();
    expect(newBtn().textContent).toContain('+ New');
    expect(newBtn().hasAttribute('aria-haspopup')).toBe(false); // plain action
    expect(caretBtn()).toBeTruthy();
    expect(caretBtn().getAttribute('aria-haspopup')).toBe('menu');
    expect(caretBtn().getAttribute('aria-expanded')).toBe('false');
    expect(caretBtn().getAttribute('aria-controls')).toBe('session-menu');
    expect(menu().hidden).toBe(true);
  });

  it('"+ New" starts a session in ONE click — no dropdown, no sessions fetch', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const props = mkProps();
    await render(props);

    await act(async () => {
      newBtn().click();
    });

    expect(props.onNewSession).toHaveBeenCalledTimes(1);
    expect(props.focusQuestionInput).toHaveBeenCalledTimes(1);
    expect(menu().hidden).toBe(true); // menu never opened
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('caret opens the dropdown (sessions fetched fresh) WITHOUT starting a session', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        ok({ sessions: [{ sessionId: 's1', label: 'freezer drill', createdAt: '2026-01-01' }] }),
      ),
    );
    const props = mkProps();
    await render(props);

    await act(async () => {
      caretBtn().click();
    });

    expect(props.onNewSession).not.toHaveBeenCalled();
    expect(menu().hidden).toBe(false);
    expect(caretBtn().getAttribute('aria-expanded')).toBe('true');
    // Dropdown still carries the default row + the session row.
    expect(container.querySelector('#session-menu-item-new')).toBeTruthy();
    expect(container.querySelector('.session-menu-item--session')?.textContent).toContain(
      'freezer drill',
    );
  });

  it("dropdown's 'New Investigation' row still starts a session and closes the menu", async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ sessions: [] })));
    const props = mkProps();
    await render(props);

    await act(async () => {
      caretBtn().click();
    });
    await act(async () => {
      (container.querySelector('#session-menu-item-new') as HTMLButtonElement).click();
    });

    expect(props.onNewSession).toHaveBeenCalledTimes(1);
    expect(props.focusQuestionInput).toHaveBeenCalledTimes(1);
    expect(menu().hidden).toBe(true);
  });

  it('session row click waits for successful rehydration before toasting', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(ok({ sessions: [{ sessionId: 's1', label: 'x' }] })),
    );
    let resolveRehydrate!: (applied: boolean) => void;
    const rehydrateResult = new Promise<boolean>((resolve) => {
      resolveRehydrate = resolve;
    });
    const props = mkProps();
    props.onRehydrate.mockReturnValue(rehydrateResult);
    await render(props);

    await act(async () => {
      caretBtn().click();
    });
    await act(async () => {
      (container.querySelector('.session-menu-item--session') as HTMLButtonElement).click();
    });

    expect(props.onRehydrate).toHaveBeenCalledWith('s1');
    expect(props.onNewSession).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalledWith('Session loaded');

    await act(async () => {
      resolveRehydrate(true);
      await rehydrateResult;
    });
    expect(showToast).toHaveBeenCalledWith('Session loaded');
  });

  it('does not claim success when rehydration returns false', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(ok({ sessions: [{ sessionId: 's1', label: 'x' }] })),
    );
    const props = mkProps();
    props.onRehydrate.mockResolvedValue(false);
    await render(props);

    await act(async () => {
      caretBtn().click();
    });
    await act(async () => {
      (container.querySelector('.session-menu-item--session') as HTMLButtonElement).click();
      await Promise.resolve();
    });

    expect(props.onRehydrate).toHaveBeenCalledWith('s1');
    expect(showToast).not.toHaveBeenCalledWith('Session loaded');
  });

  it('keyboard: Enter on the caret opens the menu', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ sessions: [] })));
    const props = mkProps();
    await render(props);

    await act(async () => {
      caretBtn().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });

    expect(menu().hidden).toBe(false);
    expect(props.onNewSession).not.toHaveBeenCalled();
  });
});
