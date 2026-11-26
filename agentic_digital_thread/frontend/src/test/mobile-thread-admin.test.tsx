// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { notifyManager } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { _resetSystemsQueryClient } from '../api/systems-query';
import { ThreadPanel } from '../shared/thread/ThreadPanel';

const THREAD_CSS = readFileSync(path.resolve(process.cwd(), 'src/styles/thread.css'), 'utf-8');
const ADMIN_CSS = readFileSync(path.resolve(process.cwd(), 'src/styles/admin.css'), 'utf-8');
const THREAD_SOURCE = readFileSync(path.resolve(process.cwd(), 'src/shared/thread/ThreadPanel.tsx'), 'utf-8');
const ADMIN_SOURCE = readFileSync(path.resolve(process.cwd(), 'src/pages/admin/AdminPage.tsx'), 'utf-8');

function blockStartingAt(source: string, start: number): string {
  const open = source.indexOf('{', start);
  if (open < 0) throw new Error('CSS block has no opening brace');
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, index);
    }
  }
  throw new Error('CSS block has no closing brace');
}

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
notifyManager.setNotifyFunction((callback) => act(callback));

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  _resetSystemsQueryClient();
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 320 });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });
  vi.restoreAllMocks();
});

describe('Thread Explorer and admin mobile CSS contracts', () => {
  it('scopes narrow Thread layouts, local heatmap scrolling, and coarse-pointer targets', () => {
    expect(THREAD_SOURCE).toContain('thread-explorer thread-mobile-safe');
    const mobile = blockStartingAt(THREAD_CSS, THREAD_CSS.indexOf('@media (max-width: 600px)'));
    expect(mobile).toMatch(/\.thread-tabs\s*\{[\s\S]*overflow-x:\s*auto/);
    expect(mobile).toMatch(/\.rule-cards\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
    expect(mobile).toMatch(/\.coverage-heatmap\s*\{[\s\S]*overflow-x:\s*auto/);
    expect(mobile).toMatch(/\.cov-grid\s*\{[\s\S]*max-width:\s*none/);
    expect(mobile).toMatch(/\.impact-body\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
    expect(mobile).toMatch(/\.impact-corr-card,[\s\S]*min-width:\s*0/);
    expect(mobile).toMatch(/\.trace-picker-bar select\s*\{[\s\S]*min-width:\s*0/);

    const coarse = blockStartingAt(THREAD_CSS, THREAD_CSS.lastIndexOf('@media (hover: none), (pointer: coarse)'));
    expect(coarse).toContain('min-height: 44px');
    expect(coarse).toContain('.thread-mobile-safe .impact-close-btn');
    expect(THREAD_CSS).toMatch(/\.thread-tab:focus-visible/);
    expect(THREAD_CSS).toMatch(/\.cov-cell:active/);
  });

  it('keeps admin chrome, settings, tables, and fleet controls within local narrow regions', () => {
    expect(ADMIN_SOURCE).toContain('id="main-content" className="admin-mobile-safe"');
    const mobile = blockStartingAt(ADMIN_CSS, ADMIN_CSS.lastIndexOf('@media (max-width: 700px)'));
    expect(mobile).toMatch(/\.admin-mobile-safe \.tab-bar\s*\{[\s\S]*overflow-x:\s*auto/);
    expect(mobile).toMatch(/\.admin-mobile-safe \.tab-panel\s*\{\s*padding:/);
    expect(mobile).toMatch(/\.admin-mobile-safe \.table-wrap table\s*\{\s*min-width:/);
    expect(mobile).toMatch(/\.admin-mobile-safe \.agents-time-filter[\s\S]*overflow-x:\s*auto/);
    expect(mobile).toMatch(/#agents-grid-log-panel[\s\S]*width:\s*100%\s*!important/);
    expect(ADMIN_CSS).toMatch(/\.admin-mobile-safe \.table-wrap\s*\{[\s\S]*overflow-x:\s*auto/);
    expect(ADMIN_CSS).toMatch(/\.quick-link:focus-visible/);

    const coarse = blockStartingAt(ADMIN_CSS, ADMIN_CSS.lastIndexOf('@media (hover: none), (pointer: coarse)'));
    expect(coarse).toContain('min-height: 44px');
    expect(coarse).toContain('.admin-mobile-safe .user-menu-btn');
  });

  it('preserves admin hash activation and contractual tab/link IDs', () => {
    expect(ADMIN_SOURCE).toContain("history.replaceState(null, '', '#' + tabId)");
    for (const id of ['tab-agents', 'tab-settings', 'tab-demoscript', 'tab-thread']) {
      expect(ADMIN_SOURCE).toContain(`data-tab="${id}"`);
      expect(ADMIN_SOURCE).toContain(`id="${id}"`);
    }
    for (const id of [
      'link-highbyte',
      'link-erp-dashboard',
      'link-console-start',
      'link-i3x-highbyte',
      'link-i3x-sitewise',
    ]) {
      expect(ADMIN_SOURCE).toContain(`id="${id}"`);
    }
  });
});

describe('ThreadPanel tablist at a 320px layout', () => {
  it('retains roving selection, focus, and linked tabpanels', async () => {
    const fetchFn = vi.fn(async (url: string) => {
      if (url === '/api/systems') return response({ systems: [{ name: 'Pending', status: 'pending' }] });
      if (url === '/api/onboard/concepts') return response({ concepts: [] });
      throw new Error(`unexpected URL: ${url}`);
    });

    await act(async () => {
      root.render(<ThreadPanel fetchFn={fetchFn} />);
    });
    await vi.waitFor(() => expect(document.getElementById('topo-loading')).toBeNull());

    expect(container.querySelector('.thread-mobile-safe')).toBeTruthy();
    const tablist = container.querySelector<HTMLElement>('.thread-tabs[role="tablist"]')!;
    const tabs = Array.from(tablist.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const panels = Array.from(container.querySelectorAll<HTMLElement>('[role="tabpanel"]'));
    expect(tabs).toHaveLength(3);
    expect(panels).toHaveLength(3);

    tabs[0].focus();
    act(() => {
      tabs[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }));
    });
    expect(document.activeElement).toBe(tabs[1]);
    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(tabs[1].tabIndex).toBe(0);
    expect(tabs[0].tabIndex).toBe(-1);
    expect(tabs[1].getAttribute('aria-controls')).toBe(panels[1].id);
    expect(panels[1].getAttribute('aria-labelledby')).toBe(tabs[1].id);
    expect(panels[1].style.display).not.toBe('none');

    act(() => {
      tabs[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true, cancelable: true }));
    });
    expect(document.activeElement).toBe(tabs[2]);
    expect(tabs[2].getAttribute('aria-selected')).toBe('true');
  });
});
