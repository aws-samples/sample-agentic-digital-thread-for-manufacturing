// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — MemoryPanel session-entry UX + record delete (memory-suite stage):
 * session entries render as REAL <button>s; clicking one opens the chat panel
 * when it is collapsed (via window.toggleChatSidebar — the AppShell
 * auto-expand mechanism) and rehydrates the session; semantic records render
 * a trash affordance that confirms (app dialog), DELETEs, and force-reloads
 * the tab. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MemoryPanel, openChatPanelIfCollapsed } from '../chat/MemoryPanel';
import { _resetMemoryCache } from '../chat/memory-cache';
import { answerConfirm, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  _resetMemoryCache();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  delete window.currentSessionId;
  delete window.toggleChatSidebar;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.getElementById('chat-sidebar')?.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function render(el: React.ReactElement) {
  await act(async () => {
    root.render(el);
  });
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function mountChatSidebar(collapsed: boolean): HTMLDivElement {
  const el = document.createElement('div');
  el.id = 'chat-sidebar';
  el.className = `chat-sidebar${collapsed ? ' collapsed' : ''}`;
  document.body.appendChild(el);
  return el;
}

const PANEL_PROPS = {
  refreshToken: 0,
  collapsed: false,
  onToggleCollapsed: () => {},
};

describe('openChatPanelIfCollapsed', () => {
  it('drives window.toggleChatSidebar ONLY when the chat panel is collapsed', () => {
    const toggle = vi.fn();
    window.toggleChatSidebar = toggle;

    mountChatSidebar(true);
    openChatPanelIfCollapsed();
    expect(toggle).toHaveBeenCalledTimes(1);

    document.getElementById('chat-sidebar')!.className = 'chat-sidebar'; // open
    openChatPanelIfCollapsed();
    expect(toggle).toHaveBeenCalledTimes(1); // no double-toggle when open
  });
});

describe('MemoryPanel — session entries', () => {
  it('renders sessions as real <button>s and click opens chat + rehydrates', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok({ sessions: [{ sessionId: 's1', label: 'freezer drill', createdAt: '2026-01-01' }] }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const toggle = vi.fn();
    window.toggleChatSidebar = toggle;
    mountChatSidebar(true); // chat starts collapsed
    const onRehydrate = vi.fn();

    await render(<MemoryPanel {...PANEL_PROPS} onRehydrate={onRehydrate} />);

    const entry = container.querySelector('.memory-session') as HTMLElement;
    expect(entry).toBeTruthy();
    expect(entry.tagName).toBe('BUTTON'); // real button — a11y + pointer cursor
    expect(entry.getAttribute('data-session-id')).toBe('s1');

    await act(async () => {
      entry.click();
    });
    expect(toggle).toHaveBeenCalledTimes(1); // chat panel opened
    expect(onRehydrate).toHaveBeenCalledWith('s1');
  });

  it('does not toggle an already-open chat panel on click', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(ok({ sessions: [{ sessionId: 's1', label: 'x' }] })),
    );
    const toggle = vi.fn();
    window.toggleChatSidebar = toggle;
    mountChatSidebar(false); // chat already open
    const onRehydrate = vi.fn();

    await render(<MemoryPanel {...PANEL_PROPS} onRehydrate={onRehydrate} />);
    await act(async () => {
      (container.querySelector('.memory-session') as HTMLElement).click();
    });
    expect(toggle).not.toHaveBeenCalled();
    expect(onRehydrate).toHaveBeenCalledWith('s1');
  });
});


  it('renders a shared retryable error and Retry recovers the sessions tab', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 503,
        statusText: 'Unavailable',
        text: async () => JSON.stringify({ detail: 'memory offline' }),
      } as Response)
      .mockResolvedValueOnce(ok({ sessions: [{ sessionId: 's2', label: 'Recovered' }] }));
    vi.stubGlobal('fetch', fetchMock);

    await render(<MemoryPanel {...PANEL_PROPS} onRehydrate={() => {}} />);
    await vi.waitFor(() => {
      expect(container.querySelector('.async-state--error')?.textContent).toContain(
        'Memory unavailable',
      );
    });

    await act(async () => {
      (container.querySelector('.async-state--error button') as HTMLButtonElement).click();
    });
    await vi.waitFor(() => expect(container.textContent).toContain('Recovered'));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

describe('MemoryPanel — record delete affordance', () => {
  async function renderFactsTab(fetchMock: ReturnType<typeof vi.fn>) {
    window.currentSessionId = 'sess-1';
    vi.stubGlobal('fetch', fetchMock);
    await render(<MemoryPanel {...PANEL_PROPS} onRehydrate={() => {}} />);
    // Switch to the Facts tab (session tab loads first).
    await act(async () => {
      (container.querySelector('[data-tab="facts"]') as HTMLElement).click();
    });
  }

  it('confirm → DELETE /api/memory/record → forced reload of the tab', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: [] })) // initial sessions tab
      .mockResolvedValueOnce(ok({ items: [{ id: 'rec-1', content: 'a fact' }] })) // facts
      .mockResolvedValueOnce(ok({ deleted: true, id: 'rec-1' })) // DELETE
      .mockResolvedValueOnce(ok({ items: [] })); // forced reload
    await renderFactsTab(fetchMock);

    const del = container.querySelector('.memory-del-btn') as HTMLElement;
    expect(del).toBeTruthy();
    await act(async () => {
      del.click();
    });

    // Nothing is deleted until the app dialog is confirmed.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(confirmText()).toEqual({
      title: 'Remove memory record?',
      body: 'The agent will no longer use it.',
      buttons: ['Cancel', 'Remove record'],
    });
    await answerConfirm('confirm');
    expect(fetchMock).toHaveBeenCalledWith('/api/memory/record?record_id=rec-1', {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    // Forced reload after the mutation — the tab now shows the empty state.
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(container.textContent).toContain('No facts yet');
  });

  it('confirm=false leaves the record untouched', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: [] }))
      .mockResolvedValueOnce(ok({ items: [{ id: 'rec-1', content: 'a fact' }] }));
    await renderFactsTab(fetchMock);

    await act(async () => {
      (container.querySelector('.memory-del-btn') as HTMLElement).click();
    });
    await answerConfirm('cancel');
    expect(fetchMock).toHaveBeenCalledTimes(2); // no DELETE, no reload
    expect(container.textContent).toContain('a fact');
  });

  it('renders NO delete affordance for records without an id', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: [] }))
      .mockResolvedValueOnce(ok({ items: [{ content: 'legacy fact, no id' }] }));
    await renderFactsTab(fetchMock);

    expect(container.textContent).toContain('legacy fact, no id');
    expect(container.querySelector('.memory-del-btn')).toBeNull();
  });

  it('keeps the record and shows a visible shared error when delete fails', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ sessions: [] }))
      .mockResolvedValueOnce(ok({ items: [{ id: 'rec-1', content: 'keep this fact' }] }))
      .mockResolvedValueOnce({
        ok: false,
        status: 503,
        statusText: 'Unavailable',
        text: async () => JSON.stringify({ detail: 'delete failed' }),
      } as Response);
    await renderFactsTab(fetchMock);

    await act(async () => {
      (container.querySelector('.memory-del-btn') as HTMLElement).click();
    });
    await answerConfirm('confirm');

    expect(container.textContent).toContain('keep this fact');
    expect(container.querySelector('.memory-delete-error.async-state--error')?.textContent).toContain(
      'Could not remove memory record',
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

});
