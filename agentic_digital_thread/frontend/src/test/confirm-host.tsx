// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Test helper: mount the app's <ConfirmHost/> (askConfirm prompts) in its own
 * root for the duration of each test, and answer the open prompt by clicking
 * its real buttons — the way a user does. Call installConfirmHost() at the top
 * level of a jsdom test file. Without a mounted host askConfirm falls back to
 * window.confirm (confirm-store.ts), which these tests deliberately avoid. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect } from 'vitest';

import { ConfirmHost } from '../shared/ui/ConfirmHost';

const HOST_ID = 'confirm-host-under-test';

export function installConfirmHost(): void {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    container.id = HOST_ID;
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<ConfirmHost />));
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });
}

/** The open app confirm dialog (rendered by the host), or null. */
export function confirmDialog(): HTMLElement | null {
  return document.querySelector<HTMLElement>(`#${HOST_ID} .confirm-dialog-overlay[role="dialog"]`);
}

/** Title, body and button labels of the open dialog. */
export function confirmText(): { title: string; body: string; buttons: string[] } {
  const dialog = confirmDialog();
  expect(dialog, 'an app confirm dialog is open').not.toBeNull();
  return {
    title: dialog!.querySelector('.confirm-dialog-title')?.textContent ?? '',
    body: dialog!.querySelector('.confirm-dialog-body')?.textContent ?? '',
    buttons: Array.from(dialog!.querySelectorAll('.confirm-dialog-actions button')).map(
      (button) => button.textContent ?? '',
    ),
  };
}

/** Click the open dialog's confirm (last) or Cancel button and flush the
 * awaiting handler. */
export async function answerConfirm(answer: 'confirm' | 'cancel'): Promise<void> {
  const dialog = confirmDialog();
  expect(dialog, 'an app confirm dialog is open').not.toBeNull();
  const button = dialog!.querySelector<HTMLButtonElement>(
    answer === 'cancel' ? '.confirm-dialog-cancel' : '.confirm-dialog-actions button:last-child',
  );
  await act(async () => {
    button!.click();
  });
}

/** Start an askConfirm-gated imperative action, answer its dialog, and
 * return the action's result once it settles. */
export async function runConfirmed<T>(
  action: () => Promise<T>,
  answer: 'confirm' | 'cancel',
): Promise<T> {
  let pending!: Promise<T>;
  await act(async () => {
    pending = action();
  });
  await answerConfirm(answer);
  let result!: T;
  await act(async () => {
    result = await pending;
  });
  return result;
}
