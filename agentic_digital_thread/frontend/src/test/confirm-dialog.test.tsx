// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* <ConfirmDialog/> — every consumer (Workshop Place clock, Vocabulary
 * placement check, thread rule deactivation) gets the standard modal
 * presentation: a backdrop overlay + centred card, role=dialog/aria-modal,
 * Escape and click-outside cancel. Regression: the dialog once rendered
 * inline with no overlay class (attendee-clock-live walk, step 4). */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfirmDialog } from '../shared/ui/ConfirmDialog';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STYLES_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../styles');
const baseCss = readFileSync(path.join(STYLES_DIR, 'base.css'), 'utf-8');
const workshopCss = readFileSync(path.join(STYLES_DIR, 'workshop.css'), 'utf-8');

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(props: { open?: boolean; tone?: 'danger' | 'default' } = {}) {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  act(() =>
    root.render(
      <ConfirmDialog
        open={props.open ?? true}
        title="Place the plant clock?"
        confirmLabel="Place clock"
        tone={props.tone}
        onCancel={onCancel}
        onConfirm={onConfirm}
      >
        <p id="impact">The bearing alarm moves.</p>
      </ConfirmDialog>,
    ),
  );
  return { onCancel, onConfirm };
}

function dialog(): HTMLElement {
  return document.querySelector('[role="dialog"]') as HTMLElement;
}
function button(label: string): HTMLButtonElement {
  return Array.from(dialog().querySelectorAll('button')).find((b) => b.textContent === label)!;
}

describe('ConfirmDialog — standard modal presentation', () => {
  it('renders the backdrop overlay as the dialog root with the centred card inside', () => {
    render();
    const surface = dialog();
    expect(surface).not.toBeNull();
    expect(surface.classList.contains('confirm-dialog-overlay')).toBe(true);
    expect(surface.getAttribute('aria-modal')).toBe('true');
    const card = surface.querySelector(':scope > .confirm-dialog');
    expect(card).not.toBeNull();
    expect(card!.querySelector('#impact')).not.toBeNull();
  });

  it('is labelled by its visible title, which appears once', () => {
    render();
    const labelId = dialog().getAttribute('aria-labelledby')!;
    const title = document.getElementById(labelId)!;
    expect(title.tagName).toBe('H3');
    expect(title.textContent).toBe('Place the plant clock?');
    expect(dialog().textContent!.split('Place the plant clock?')).toHaveLength(2);
  });

  it('renders nothing while closed', () => {
    render({ open: false });
    expect(dialog()).toBeNull();
    expect(document.querySelector('.confirm-dialog-overlay')).toBeNull();
  });

  it('Escape cancels without confirming', () => {
    const { onCancel, onConfirm } = render();
    act(() => {
      button('Cancel').focus();
      document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('a click on the backdrop cancels; a click inside the card does not', () => {
    const { onCancel } = render();
    act(() => (dialog().querySelector('.confirm-dialog') as HTMLElement).click());
    expect(onCancel).not.toHaveBeenCalled();
    act(() => dialog().click());
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('Cancel and confirm buttons route to their callbacks, confirm last', () => {
    const { onCancel, onConfirm } = render();
    const buttons = Array.from(dialog().querySelectorAll('button'));
    expect(buttons.map((b) => b.textContent)).toEqual(['Cancel', 'Place clock']);
    act(() => button('Place clock').click());
    expect(onConfirm).toHaveBeenCalledTimes(1);
    act(() => button('Cancel').click());
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('a backdrop click that is the second of a double-click does NOT cancel', () => {
    // The first click of a double-click opens the dialog; the second lands on
    // the backdrop and must not dismiss it.
    const { onCancel } = render();
    act(() => {
      dialog().dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 2 }));
    });
    expect(onCancel).not.toHaveBeenCalled();
    act(() => {
      dialog().dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('takes an optional cancel label (default "Cancel")', () => {
    act(() =>
      root.render(
        <ConfirmDialog
          open
          title="Stop the answer?"
          confirmLabel="Stop answer"
          cancelLabel="Abbrechen"
          onCancel={() => undefined}
          onConfirm={() => undefined}
        >
          <p>Body</p>
        </ConfirmDialog>,
      ),
    );
    expect(Array.from(dialog().querySelectorAll('button')).map((b) => b.textContent)).toEqual([
      'Abbrechen',
      'Stop answer',
    ]);
  });

  it('danger tone keeps the confirm-danger-btn hook; default uses the shared primary button', () => {
    render({ tone: 'danger' });
    expect(button('Place clock').classList.contains('confirm-danger-btn')).toBe(true);
    render({ tone: 'default' });
    expect(button('Place clock').classList.contains('btn-primary')).toBe(true);
  });
});

describe('ConfirmDialog / disabled-button CSS contract', () => {
  it('base.css positions the overlay as a fixed, centred backdrop', () => {
    const block = baseCss.slice(baseCss.indexOf('.confirm-dialog-overlay {'));
    const body = block.slice(0, block.indexOf('}'));
    expect(body).toMatch(/position:\s*fixed/);
    expect(body).toMatch(/inset:\s*0/);
    expect(body).toMatch(/background:\s*rgba\(/);
    expect(body).toMatch(/align-items:\s*center/);
    expect(body).toMatch(/justify-content:\s*center/);
  });

  /* WCAG 2.x relative luminance + contrast ratio. */
  function lum(hex: string): number {
    const [r, g, b] = [1, 3, 5]
      .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  function contrast(a: string, b: string): number {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  }
  function token(block: string, name: string): string {
    const m = block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
    if (!m) throw new Error(`${name} not found`);
    return m[1];
  }

  it('the disabled Workshop button is not faded and its label clears 3:1 in both themes', () => {
    const rule = workshopCss.slice(workshopCss.indexOf('.workshop-place-row button:disabled {'));
    const body = rule.slice(0, rule.indexOf('}'));
    expect(body).toMatch(/opacity:\s*1/);
    expect(body).toMatch(/color:\s*var\(--muted\)/);
    expect(body).toMatch(/background:\s*var\(--surface2\)/);

    const dark = baseCss.slice(baseCss.indexOf(':root {'));
    const light = baseCss.slice(baseCss.indexOf(':root.light {'));
    for (const theme of [dark, light]) {
      expect(contrast(token(theme, '--muted'), token(theme, '--surface2'))).toBeGreaterThanOrEqual(3);
    }
  });
});
