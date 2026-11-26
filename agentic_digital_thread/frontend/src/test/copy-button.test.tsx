// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <CopyButton/> / <CopyablePre/> pins:
 * - a real <button type="button"> with an explicit aria-label (keyboard
 *   reachable, focus-ring class .adt-copy-btn);
 * - copies the EXACT untruncated text via navigator.clipboard.writeText;
 * - shows a brief "Copied" confirmation that REVERTS to "Copy";
 * - Clipboard API unavailable/rejecting ⇒ hidden-textarea execCommand
 *   fallback — and when BOTH paths fail the button reports "Copy failed",
 *   never a pretended success;
 * - <CopyablePre/> keeps the block a LITERAL text <pre> (zero innerHTML). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CopyButton, CopyablePre } from '../chat/CopyButton';
import { setLang } from '../lib/i18n';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
  setClipboard(undefined);
  delete (document as { execCommand?: unknown }).execCommand;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

/** Install (or remove) a stub Clipboard API — jsdom has none by default. */
function setClipboard(writeText: ((t: string) => Promise<void>) | undefined) {
  Object.defineProperty(window.navigator, 'clipboard', {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  });
}

/** Install a stub document.execCommand (jsdom has none). The stub captures
 * the value of the temporary textarea present at call time. */
function setExecCommand(result: boolean | (() => boolean)) {
  const captured: string[] = [];
  const fn = vi.fn((command: string) => {
    expect(command).toBe('copy');
    const ta = document.querySelector('textarea');
    captured.push(ta ? ta.value : '<no textarea>');
    return typeof result === 'function' ? result() : result;
  });
  (document as unknown as { execCommand: typeof fn }).execCommand = fn;
  return { fn, captured };
}

function button(): HTMLButtonElement {
  return container.querySelector<HTMLButtonElement>('button.adt-copy-btn')!;
}

async function click() {
  await act(async () => {
    button().click();
  });
}

const TEXT = 'SELECT a FROM t;\n-- second line with <script>alert(1)</script>';

describe('<CopyButton/>', () => {
  it('renders a real <button type="button"> with the aria-label', () => {
    render(<CopyButton text={TEXT} label="Copy executed query" />);
    const btn = button();
    expect(btn).not.toBeNull();
    expect(btn.getAttribute('type')).toBe('button');
    expect(btn.getAttribute('aria-label')).toBe('Copy executed query');
    expect(btn.textContent).toBe('Copy');
  });

  it('copies the EXACT text via the Clipboard API, shows Copied, then reverts', async () => {
    vi.useFakeTimers();
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard(writeText);
    render(<CopyButton text={TEXT} label="copy" />);

    await click();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(TEXT);
    expect(button().textContent).toBe('Copied');

    act(() => {
      vi.advanceTimersByTime(1600);
    });
    expect(button().textContent).toBe('Copy');
  });

  it('falls back to execCommand with the exact text when the Clipboard API is unavailable', async () => {
    setClipboard(undefined);
    const { fn, captured } = setExecCommand(true);
    render(<CopyButton text={TEXT} label="copy" />);

    await click();
    expect(fn).toHaveBeenCalledTimes(1);
    expect(captured).toEqual([TEXT]);
    expect(button().textContent).toBe('Copied');
    // the temporary textarea is cleaned up
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('a REJECTING Clipboard API falls back to execCommand', async () => {
    setClipboard(() => Promise.reject(new Error('denied')));
    const { fn } = setExecCommand(true);
    render(<CopyButton text={TEXT} label="copy" />);

    await click();
    expect(fn).toHaveBeenCalledTimes(1);
    expect(button().textContent).toBe('Copied');
  });

  it('reports failure honestly when BOTH paths fail (execCommand false)', async () => {
    setClipboard(undefined);
    setExecCommand(false);
    render(<CopyButton text={TEXT} label="copy" />);

    await click();
    expect(button().textContent).toBe('Copy failed');
    expect(button().textContent).not.toBe('Copied');
  });

  it('reports failure honestly when execCommand THROWS (never throws out)', async () => {
    setClipboard(undefined);
    setExecCommand(() => {
      throw new Error('boom');
    });
    render(<CopyButton text={TEXT} label="copy" />);

    await click(); // must not throw
    expect(button().textContent).toBe('Copy failed');
  });
});

describe('<CopyButton/> i18n', () => {
  // lib/i18n persists through localStorage (broken in this node/jsdom combo —
  // same Map-backed stub as i18n-chrome.test.tsx).
  let store: Map<string, string>;

  beforeEach(() => {
    store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
    });
  });

  afterEach(() => {
    act(() => setLang('en')); // langStore is module state — reset for siblings
    vi.unstubAllGlobals();
  });

  it('state text follows the chrome language (ja): Copy → Copied translated', async () => {
    act(() => setLang('ja'));
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard(writeText);
    render(<CopyButton text={TEXT} label="copy" />);
    expect(button().textContent).toBe('コピー');

    await click();
    expect(button().textContent).toBe('コピーしました');
  });

  it('the failure state is translated too (ja: both copy paths fail)', async () => {
    act(() => setLang('ja'));
    setClipboard(undefined);
    setExecCommand(false);
    render(<CopyButton text={TEXT} label="copy" />);

    await click();
    expect(button().textContent).toBe('コピーに失敗しました');
  });
});

describe('<CopyablePre/>', () => {
  it('renders a literal-text <pre> (hostile markup inert) with a copy button for the exact text', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard(writeText);
    render(
      <CopyablePre
        text={TEXT}
        className="evidence-executed-query"
        preStyle={{ fontSize: '0.65rem' }}
        copyLabel="Copy executed query"
      />,
    );

    const pre = container.querySelector('pre.evidence-executed-query')!;
    expect(pre).not.toBeNull();
    expect(pre.textContent).toBe(TEXT);
    // zero-innerHTML: the hostile markup never becomes elements
    expect(container.querySelector('script,img')).toBeNull();

    await click();
    expect(writeText).toHaveBeenCalledWith(TEXT);
  });
});
