// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the chat modal's centered reading column (UI improvement A).
 *
 * Pins (each names the mutation it kills):
 * 1. `#modal-messages .chat-content-column` present and hosting the thread —
 *    kills deleting the column wrapper from the modal markup.
 * 2. The sidebar (ChatSidebar.tsx) renders NO chat-content-column — kills
 *    leaking the modal-only wrapper into the sidebar (#messages).
 * 3. dashboard.css carries the `.chat-modal .messages .chat-content-column`
 *    rule (max-width + centering) and no `.chat-content-column` rule outside
 *    a .chat-modal scope — kills deleting the CSS while keeping the class
 *    (a class with no rule renders exactly the old dead-bubble layout). */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { act, useRef, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatModal } from '../chat/ChatModal';

vi.mock('../chat/PromptLibrary', () => ({
  PromptLibrary: () => <button type="button" className="prompt-library-btn">Prompts</button>,
}));

vi.mock('../views/onboard/components/PersonaGroups', () => ({
  PersonaGroups: () => <div>Persona suggestions</div>,
}));

vi.mock('../chat/useAskStream', async () => {
  const { createThreadStore } = await import('../chat/thread-store');
  return { chatThreadStore: createThreadStore() };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const DASHBOARD_CSS = readFileSync(
  path.resolve(TEST_DIR, '../styles/dashboard.css'),
  'utf-8',
);
const CHAT_SIDEBAR_SRC = readFileSync(
  path.resolve(TEST_DIR, '../chat/ChatSidebar.tsx'),
  'utf-8',
);

let container: HTMLDivElement;
let root: Root;

function Harness() {
  const [open, setOpen] = useState(false);
  const modalInputRef = useRef<HTMLTextAreaElement>(null);
  return (
    <>
      <button id="chat-trigger" type="button" onClick={() => setOpen(true)}>
        Open chat
      </button>
      <ChatModal
        open={open}
        streaming={false}
        onClose={() => setOpen(false)}
        onNewSession={() => {}}
        onSendModal={() => {}}
        onStop={() => {}}
        onThreadClick={() => {}}
        onCompose={() => {}}
        modalInputRef={modalInputRef}
      />
    </>
  );
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  window.refreshSuggestions = vi.fn();
  act(() => root.render(<Harness />));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function openModal(): void {
  const trigger = document.getElementById('chat-trigger') as HTMLButtonElement;
  act(() => trigger.click());
}

describe('chat modal reading column (A)', () => {
  it('wraps the modal thread in .chat-content-column inside #modal-messages', () => {
    openModal();
    const column = document.querySelector('#modal-messages > .chat-content-column');
    expect(column).not.toBeNull();
    // The thread itself renders INSIDE the column (its status region is the
    // stable empty-thread marker).
    expect(column!.querySelector('.chat-stream-status')).not.toBeNull();
  });

  it('does not leak the column wrapper into the sidebar thread host', () => {
    // The sidebar renders <ChatThread/> directly inside #messages — the
    // reading column is a MODAL-ONLY treatment.
    expect(CHAT_SIDEBAR_SRC.includes('chat-content-column')).toBe(false);
  });

  it('dashboard.css caps and centers the column, scoped to the modal only', () => {
    const rule = DASHBOARD_CSS.match(
      /\.chat-modal \.messages \.chat-content-column\s*\{([^}]*)\}/,
    );
    expect(rule).not.toBeNull();
    // Expanded modal: generous-but-finite width so wide tables get room
    // (prose readability comes from thread.css's 72ch flowing-text cap).
    expect(rule![1]).toContain('max-width: 1700px');
    expect(rule![1]).toContain('margin: 0 auto');
    // The persona pills share the same column.
    expect(DASHBOARD_CSS).toMatch(
      /\.chat-modal \.persona-panel \.persona-groups-container\s*\{[^}]*max-width: 86ch/,
    );
    // No .chat-content-column selector reachable outside a .chat-modal scope.
    const selectors = DASHBOARD_CSS.match(/[^{}]*\.chat-content-column[^{]*\{/g) || [];
    expect(selectors.length).toBeGreaterThan(0);
    for (const sel of selectors) {
      expect(sel).toContain('.chat-modal');
    }
  });
});
