// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
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

function openModal(): HTMLButtonElement {
  const trigger = document.getElementById('chat-trigger') as HTMLButtonElement;
  trigger.focus();
  act(() => trigger.click());
  return trigger;
}

function pressKey(target: Element, key: string, shiftKey = false) {
  act(() => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true }));
  });
}

describe('<ChatModal/> accessibility', () => {
  it('stays mounted but inert while closed, then opens as a labelled modal with contractual markup', () => {
    const closedOverlay = document.getElementById('chat-modal-overlay')!;
    expect(closedOverlay.classList.contains('open')).toBe(false);
    expect(closedOverlay.getAttribute('role')).toBeNull();
    expect(closedOverlay.getAttribute('aria-hidden')).toBe('true');
    expect(closedOverlay.hasAttribute('inert')).toBe(true);
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    openModal();
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(dialog.id).toBe('chat-modal-overlay');
    expect(dialog.className).toBe('chat-modal-overlay open');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('chat-modal-title');
    expect(document.getElementById('chat-modal-title')?.textContent).toBe('Orchestrator');
    expect(dialog.querySelector('.chat-modal')).not.toBeNull();
    expect(document.getElementById('modal-question-input')).not.toBeNull();
    expect(document.getElementById('modal-send-btn')).not.toBeNull();
  });

  it('keeps the existing composer initial-focus behavior', () => {
    openModal();
    expect(document.activeElement?.id).toBe('modal-question-input');
  });

  it('traps Tab and Shift+Tab within the modal controls', () => {
    openModal();
    const first = document.querySelector<HTMLButtonElement>('.chat-modal-action')!;
    const last = document.getElementById('modal-send-btn') as HTMLButtonElement;

    last.focus();
    pressKey(last, 'Tab');
    expect(document.activeElement).toBe(first);

    first.focus();
    pressKey(first, 'Tab', true);
    expect(document.activeElement).toBe(last);
  });

  it('closes on Escape and restores focus to the pop-out trigger', () => {
    const trigger = openModal();
    const input = document.getElementById('modal-question-input') as HTMLTextAreaElement;
    pressKey(input, 'Escape');

    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('preserves overlay-click dismissal', () => {
    const trigger = openModal();
    const overlay = document.getElementById('chat-modal-overlay')!;
    act(() => overlay.dispatchEvent(new MouseEvent('click', { bubbles: true })));

    const closedOverlay = document.getElementById('chat-modal-overlay')!;
    expect(closedOverlay.classList.contains('open')).toBe(false);
    expect(closedOverlay.getAttribute('aria-hidden')).toBe('true');
    expect(closedOverlay.hasAttribute('inert')).toBe(true);
    expect(document.activeElement).toBe(trigger);
  });
});
