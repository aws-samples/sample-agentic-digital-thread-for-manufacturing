// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The removal confirmation is an accessible dialog.
 *
 * The "Remove registration" (and ordinary "Remove system") overlay renders
 * through the shared ModalSurface/useDialogFocus: it is role="dialog" named by
 * its heading, moves initial focus to the type-to-confirm input (a real
 * <label htmlFor>), traps Tab, closes on Escape, and returns focus to the
 * element that opened it on cancel. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ObModals } from '../views/onboard/components/ObModals';
import { closeModal, flowStore, openModal } from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function openDisconnect(registration = true) {
  act(() => {
    root.render(<ObModals />);
  });
  act(() => {
    openModal({ kind: 'disconnect', systemId: 'quality-lab', systemName: 'Quality Lab', registration });
  });
}

function dialog(): HTMLElement {
  return document.querySelector('[role="dialog"]') as HTMLElement;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  flowStore.set({ ...flowStore.get(), modal: null });
});

afterEach(() => {
  act(() => {
    closeModal();
    root.unmount();
  });
  container.remove();
});

describe('the removal confirmation dialog', () => {
  it('is a dialog named by its heading', () => {
    openDisconnect();
    const d = dialog();
    expect(d).not.toBeNull();
    expect(d.getAttribute('aria-modal')).toBe('true');
    const labelledBy = d.getAttribute('aria-labelledby');
    expect(labelledBy).toBeTruthy();
    const heading = document.getElementById(labelledBy!);
    expect(heading?.textContent).toContain('Remove registration');
  });

  it('moves initial focus to the type-to-confirm input, which has a real label', () => {
    openDisconnect();
    const input = document.getElementById('ob-disc-confirm-input') as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    const label = document.querySelector('label[for="ob-disc-confirm-input"]');
    expect(label).not.toBeNull();
  });

  it('closes on Escape', () => {
    openDisconnect();
    expect(dialog()).not.toBeNull();
    act(() => {
      dialog().dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
    });
    expect(flowStore.get().modal).toBeNull();
  });

  it('returns focus to the opener on cancel', () => {
    const opener = document.createElement('button');
    opener.textContent = 'Remove registration';
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    openDisconnect();
    expect(document.activeElement).not.toBe(opener); // focus moved into the dialog
    act(() => {
      (document.getElementById('ob-disc-no') as HTMLButtonElement).click();
    });
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('traps Tab within the dialog: last wraps to first, first wraps to last', () => {
    openDisconnect(false);
    const d = dialog();
    const focusables = Array.from(
      d.querySelectorAll<HTMLElement>('button, input'),
    ).filter((el) => !(el as HTMLButtonElement | HTMLInputElement).disabled);
    expect(focusables.length).toBeGreaterThan(1);
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    last.focus();
    act(() => {
      last.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    });
    expect(document.activeElement).toBe(first);

    first.focus();
    act(() => {
      first.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }),
      );
    });
    expect(document.activeElement).toBe(last);
  });
});
