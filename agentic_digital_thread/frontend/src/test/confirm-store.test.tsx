// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* askConfirm() + <ConfirmHost/> (shared/ui/confirm-store.ts, ConfirmHost.tsx):
 * the app's replacement for the native confirm(). Pins: resolve true/false
 * from the real buttons, queueing, duplicate suppression, Escape cancels,
 * focus returns to the trigger, danger tone, the window.confirm fallback when
 * no host is mounted, host unmount resolving pending requests false, the
 * double-click backdrop guard, and literal-text paragraphs. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfirmHost } from '../shared/ui/ConfirmHost';
import { askConfirm, confirmStore } from '../shared/ui/confirm-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root | null;

function mountHost(): void {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(<ConfirmHost />));
}

function unmountHost(): void {
  if (!root) return;
  act(() => root!.unmount());
  root = null;
  container.remove();
}

afterEach(() => {
  unmountHost();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

function dialogs(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.confirm-dialog-overlay[role="dialog"]'));
}
function dialog(): HTMLElement {
  const [only] = dialogs();
  expect(only, 'a confirm dialog is open').toBeTruthy();
  return only;
}
function button(label: string): HTMLButtonElement {
  const found = Array.from(dialog().querySelectorAll('button')).find((b) => b.textContent === label);
  expect(found, `button "${label}"`).toBeTruthy();
  return found as HTMLButtonElement;
}
async function click(el: HTMLElement): Promise<void> {
  await act(async () => {
    el.click();
  });
}

/** Open a request inside act so the host renders it; returns the promise. */
function ask(options: Parameters<typeof askConfirm>[0]): Promise<boolean> {
  let pending!: Promise<boolean>;
  act(() => {
    pending = askConfirm(options);
  });
  return pending;
}

const REMOVE = { key: 'test:remove-note:1', title: 'Remove note?', body: 'The note is gone for good.', confirmLabel: 'Remove note' };

describe('askConfirm with a mounted <ConfirmHost/>', () => {
  beforeEach(() => mountHost());

  it('renders title, body and a specific confirm label through ConfirmDialog', () => {
    void ask({ ...REMOVE, tone: 'danger' });
    const surface = dialog();
    expect(surface.getAttribute('aria-modal')).toBe('true');
    expect(surface.querySelector('.confirm-dialog-title')?.textContent).toBe('Remove note?');
    expect(surface.querySelector('.confirm-dialog-body')?.textContent).toBe('The note is gone for good.');
    expect(Array.from(surface.querySelectorAll('button')).map((b) => b.textContent)).toEqual([
      'Cancel',
      'Remove note',
    ]);
  });

  it('resolves true on the confirm button and closes', async () => {
    const result = ask(REMOVE);
    await click(button('Remove note'));
    await expect(result).resolves.toBe(true);
    expect(dialogs()).toHaveLength(0);
    expect(confirmStore.get().queue).toHaveLength(0);
  });

  it('resolves false on Cancel and closes', async () => {
    const result = ask(REMOVE);
    await click(button('Cancel'));
    await expect(result).resolves.toBe(false);
    expect(dialogs()).toHaveLength(0);
  });

  it('Escape cancels', async () => {
    const result = ask(REMOVE);
    await act(async () => {
      document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    await expect(result).resolves.toBe(false);
    expect(dialogs()).toHaveLength(0);
  });

  it('queues a second request and opens it when the first settles', async () => {
    const first = ask(REMOVE);
    const second = ask({ key: 'test:clear-all', title: 'Clear all?', body: 'Every note goes.', confirmLabel: 'Clear all' });
    expect(dialogs()).toHaveLength(1);
    expect(dialog().querySelector('.confirm-dialog-title')?.textContent).toBe('Remove note?');

    await click(button('Remove note'));
    await expect(first).resolves.toBe(true);
    expect(dialogs()).toHaveLength(1);
    expect(dialog().querySelector('.confirm-dialog-title')?.textContent).toBe('Clear all?');

    await click(button('Cancel'));
    await expect(second).resolves.toBe(false);
    expect(dialogs()).toHaveLength(0);
  });

  it('a request with the same key while one is open resolves false at once (double-click guard)', async () => {
    const first = ask(REMOVE);
    const duplicate = ask({ ...REMOVE, body: 'Different wording, same action.' });
    await expect(duplicate).resolves.toBe(false);
    expect(confirmStore.get().queue).toHaveLength(1);
    await click(button('Remove note'));
    await expect(first).resolves.toBe(true);
  });

  it('identical copy for a DIFFERENT target queues instead of being dropped', async () => {
    const first = ask(REMOVE);
    const other = ask({ ...REMOVE, key: 'test:remove-note:2' });
    expect(confirmStore.get().queue).toHaveLength(2);
    await click(button('Cancel'));
    await expect(first).resolves.toBe(false);
    expect(dialogs()).toHaveLength(1);
    await click(button('Remove note'));
    await expect(other).resolves.toBe(true);
  });

  it('a double-click on the confirm button settles only that request, never the next queued one', async () => {
    const first = ask(REMOVE);
    const second = ask({ key: 'test:clear-all', title: 'Clear all?', body: 'Every note goes.', confirmLabel: 'Clear all' });
    const confirmButton = button('Remove note');
    await act(async () => {
      confirmButton.click();
      confirmButton.click();
    });
    await expect(first).resolves.toBe(true);
    // The queued request is still open, waiting for its own answer.
    expect(dialog().querySelector('.confirm-dialog-title')?.textContent).toBe('Clear all?');
    expect(confirmStore.get().queue).toHaveLength(1);
    await click(button('Cancel'));
    await expect(second).resolves.toBe(false);
  });

  it('returns focus to the element that opened it', async () => {
    const trigger = document.createElement('button');
    trigger.textContent = 'Remove';
    document.body.appendChild(trigger);
    trigger.focus();
    const result = ask(REMOVE);
    expect(dialog().contains(document.activeElement)).toBe(true);
    await click(button('Cancel'));
    await result;
    expect(document.activeElement).toBe(trigger);
  });

  it("danger tone styles the confirm button; default uses the shared primary", async () => {
    const danger = ask({ ...REMOVE, tone: 'danger' });
    expect(button('Remove note').classList.contains('confirm-danger-btn')).toBe(true);
    await click(button('Cancel'));
    await danger;
    void ask({ key: 'test:run-suite', title: 'Run suite?', body: 'Costs money.', confirmLabel: 'Run full suite' });
    expect(button('Run full suite').classList.contains('btn-primary')).toBe(true);
    expect(button('Run full suite').classList.contains('confirm-danger-btn')).toBe(false);
  });

  it('uses a custom cancel label when given', () => {
    void ask({ ...REMOVE, cancelLabel: 'Keep it' });
    expect(Array.from(dialog().querySelectorAll('button')).map((b) => b.textContent)).toEqual([
      'Keep it',
      'Remove note',
    ]);
  });

  it('renders blank-line breaks as paragraphs of literal text (no HTML)', () => {
    void ask({ ...REMOVE, body: 'First <b>part</b>.\n\nSecond part.' });
    const paragraphs = Array.from(dialog().querySelectorAll('.confirm-dialog-body p'));
    expect(paragraphs.map((p) => p.textContent)).toEqual(['First <b>part</b>.', 'Second part.']);
    expect(dialog().querySelector('.confirm-dialog-body b')).toBeNull();
  });

  it('ignores the second click of a double-click on the backdrop; a single click cancels', async () => {
    const result = ask(REMOVE);
    await act(async () => {
      dialog().dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 2 }));
    });
    expect(dialogs()).toHaveLength(1);
    await act(async () => {
      dialog().dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    });
    await expect(result).resolves.toBe(false);
    expect(dialogs()).toHaveLength(0);
  });

  it('unmounting the host resolves pending requests false', async () => {
    const first = ask(REMOVE);
    const second = ask({ key: 'test:clear-all', title: 'Clear all?', body: 'Every note goes.', confirmLabel: 'Clear all' });
    unmountHost();
    await expect(first).resolves.toBe(false);
    await expect(second).resolves.toBe(false);
    expect(confirmStore.get().queue).toHaveLength(0);
  });

  it('a second mounted host stays idle: one dialog, not two', async () => {
    const extra = document.createElement('div');
    document.body.appendChild(extra);
    const extraRoot = createRoot(extra);
    act(() => extraRoot.render(<ConfirmHost />));
    const result = ask(REMOVE);
    expect(dialogs()).toHaveLength(1);
    await click(button('Remove note'));
    await expect(result).resolves.toBe(true);
    act(() => extraRoot.unmount());
  });

  it('never calls the native window.confirm while a host is mounted', async () => {
    const native = vi.fn(() => true);
    vi.stubGlobal('confirm', native);
    const result = ask(REMOVE);
    await click(button('Cancel'));
    await expect(result).resolves.toBe(false);
    expect(native).not.toHaveBeenCalled();
  });
});

describe('askConfirm with NO host mounted — window.confirm fallback', () => {
  it('asks the native confirm with title and body, and resolves its answer', async () => {
    const native = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await expect(askConfirm(REMOVE)).resolves.toBe(true);
    expect(native).toHaveBeenCalledWith('Remove note?\n\nThe note is gone for good.');
    native.mockReturnValue(false);
    await expect(askConfirm(REMOVE)).resolves.toBe(false);
    expect(dialogs()).toHaveLength(0);
  });
});
