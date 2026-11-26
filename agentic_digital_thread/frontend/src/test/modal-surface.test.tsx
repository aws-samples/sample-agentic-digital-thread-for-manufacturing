// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { act, useRef, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModalSurface } from '../shared/ui/ModalSurface';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function Harness({ initial = false }: { initial?: boolean }) {
  const [open, setOpen] = useState(false);
  const initialRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button id="dialog-trigger" type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <ModalSurface
        open={open}
        onClose={() => setOpen(false)}
        labelledBy="dialog-title"
        initialFocus={initial ? initialRef : undefined}
        className="existing-modal-overlay"
      >
        <h2 id="dialog-title">Accessible dialog</h2>
        <button id="dialog-first" type="button">First</button>
        <button id="dialog-initial" ref={initialRef} type="button">Initial</button>
        <button id="dialog-last" type="button">Last</button>
      </ModalSurface>
    </>
  );
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function render(initial = false) {
  act(() => root.render(<Harness initial={initial} />));
}

function openDialog() {
  const trigger = document.getElementById('dialog-trigger') as HTMLButtonElement;
  trigger.focus();
  act(() => trigger.click());
  return trigger;
}

function pressKey(target: Element, key: string, shiftKey = false) {
  act(() => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true }));
  });
}

describe('<ModalSurface/>', () => {
  it('is absent and therefore not tabbable while closed', () => {
    render();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelectorAll('button')).toHaveLength(1);
  });

  it('can preserve closed markup as aria-hidden and inert without creating a dialog', () => {
    act(() => {
      root.render(
        <ModalSurface
          id="persistent-overlay"
          open={false}
          keepMounted
          onClose={() => {}}
          labelledBy="persistent-title"
        >
          <h2 id="persistent-title">Persistent dialog</h2>
          <button id="persistent-action" type="button">Action</button>
        </ModalSurface>,
      );
    });

    const overlay = document.getElementById('persistent-overlay')!;
    expect(overlay.getAttribute('role')).toBeNull();
    expect(overlay.getAttribute('aria-modal')).toBeNull();
    expect(overlay.getAttribute('aria-labelledby')).toBeNull();
    expect(overlay.getAttribute('aria-hidden')).toBe('true');
    expect(overlay.hasAttribute('inert')).toBe(true);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('sets dialog ARIA attributes, preserves classes, and focuses the first control', () => {
    render();
    openDialog();
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-labelledby')).toBe('dialog-title');
    expect(dialog.classList.contains('existing-modal-overlay')).toBe(true);
    expect(document.activeElement?.id).toBe('dialog-first');
  });

  it('honors an initial-focus target', () => {
    render(true);
    openDialog();
    expect(document.activeElement?.id).toBe('dialog-initial');
  });

  it('cycles Tab and Shift+Tab at the dialog boundaries', () => {
    render();
    openDialog();
    const first = document.getElementById('dialog-first') as HTMLButtonElement;
    const last = document.getElementById('dialog-last') as HTMLButtonElement;

    last.focus();
    pressKey(last, 'Tab');
    expect(document.activeElement).toBe(first);

    pressKey(first, 'Tab', true);
    expect(document.activeElement).toBe(last);
  });

  it('closes on Escape and restores focus to the trigger', () => {
    render();
    const trigger = openDialog();
    const first = document.getElementById('dialog-first') as HTMLButtonElement;

    pressKey(first, 'Escape');

    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('locks body scrolling while open and restores the previous inline overflow', () => {
    document.body.style.overflow = 'clip';
    render();
    const trigger = openDialog();
    expect(document.body.style.overflow).toBe('hidden');

    pressKey(document.getElementById('dialog-first')!, 'Escape');
    expect(document.body.style.overflow).toBe('clip');
    expect(document.activeElement).toBe(trigger);
    document.body.style.overflow = '';
  });

  it('reference-counts the body lock across multiple open modal surfaces', () => {
    document.body.style.overflow = 'auto';
    const modal = (id: string) => (
      <ModalSurface id={id} open onClose={() => {}} label={id}>
        <button type="button">Action</button>
      </ModalSurface>
    );

    act(() => root.render(<>{modal('first-modal')}{modal('second-modal')}</>));
    expect(document.body.style.overflow).toBe('hidden');

    act(() => root.render(modal('second-modal')));
    expect(document.body.style.overflow).toBe('hidden');

    act(() => root.render(<></>));
    expect(document.body.style.overflow).toBe('auto');
    document.body.style.overflow = '';
  });

  it('builds aria-labelledby from the required label prop', () => {
    act(() => {
      root.render(
        <ModalSurface open onClose={() => {}} label="Generated label">
          <button type="button">Action</button>
        </ModalSurface>,
      );
    });
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    const label = document.getElementById(dialog.getAttribute('aria-labelledby')!);
    expect(label?.textContent).toBe('Generated label');
  });
});
