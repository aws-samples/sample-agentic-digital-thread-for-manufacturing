// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — investigation-trace show more/less (arch-visual-polish).
 *
 *  - traceBodyOverflows: pure overflow rule gating the affordance;
 *  - TraceEntries: the Show more button + clamped fade exist ONLY when the
 *    body measures as overflowing (scrollHeight mocked — jsdom has no
 *    layout); expanding flips button label/aria state, drops the clamp and
 *    drives the animated inline max-height from the measured height. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { TraceEntry } from '../lib/arch-store';
import TraceEntries, { TRACE_CLAMP_PX, traceBodyOverflows } from '../views/architecture/TraceEntries';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe('traceBodyOverflows — pure overflow rule', () => {
  it('is false at/near the clamp (1px tolerance) and true beyond it', () => {
    expect(traceBodyOverflows(0)).toBe(false);
    expect(traceBodyOverflows(TRACE_CLAMP_PX)).toBe(false);
    expect(traceBodyOverflows(TRACE_CLAMP_PX + 1)).toBe(false); // grazing: no dangling button
    expect(traceBodyOverflows(TRACE_CLAMP_PX + 2)).toBe(true);
    expect(traceBodyOverflows(400)).toBe(true);
  });
});

describe('TraceEntries — overflow-gated Show more/less', () => {
  let container: HTMLDivElement;
  let root: Root;
  let scrollHeightDesc: PropertyDescriptor | undefined;
  let mockedScrollHeight = 0;

  const entry = (text: string): TraceEntry => ({
    id: 1,
    type: 'decision',
    step: 1,
    label: 'Reasoning',
    text,
    question: 1,
  });

  const renderEntries = (entries: TraceEntry[]) =>
    act(() => {
      root.render(
        <TraceEntries
          entries={entries}
          questionCounter={1}
          activeQuestionFilter={null}
          onFilterQuestion={() => {}}
          visible
        />,
      );
    });

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    // jsdom computes no layout — drive the measurement through a mocked
    // Element.scrollHeight (what the component reads via its body ref).
    scrollHeightDesc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollHeight');
    Object.defineProperty(Element.prototype, 'scrollHeight', {
      configurable: true,
      get() {
        return mockedScrollHeight;
      },
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    if (scrollHeightDesc) Object.defineProperty(Element.prototype, 'scrollHeight', scrollHeightDesc);
    mockedScrollHeight = 0;
  });

  it('short entry: no Show more button, no caret, no clamped fade', () => {
    mockedScrollHeight = 40;
    renderEntries([entry('short note')]);
    expect(container.querySelector('.trace-show-toggle')).toBeNull();
    expect(container.querySelector('.trace-expand-toggle')).toBeNull();
    expect(container.querySelector('.trace-body.clamped')).toBeNull();
    expect(container.querySelector('.trace-body')).not.toBeNull(); // body still renders
  });

  it('overflowing entry: clamped fade + Show more; expanding animates and flips to Show less', () => {
    mockedScrollHeight = 400;
    renderEntries([entry('a very long trace body')]);

    const button = container.querySelector<HTMLButtonElement>('.trace-show-toggle');
    expect(button).not.toBeNull();
    expect(button!.tagName).toBe('BUTTON'); // keyboard accessible
    expect(button!.textContent).toBe('Show more');
    expect(button!.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('.trace-body.clamped')).not.toBeNull();

    act(() => button!.click());

    const body = container.querySelector<HTMLElement>('.trace-body')!;
    expect(body.classList.contains('expanded')).toBe(true);
    expect(body.classList.contains('clamped')).toBe(false); // fade only while collapsed
    // Animated target: inline max-height carries the measured content height
    // (an explicit px value the CSS transition can run against).
    expect(body.style.maxHeight).toBe('400px');
    const after = container.querySelector<HTMLButtonElement>('.trace-show-toggle')!;
    expect(after.textContent).toBe('Show less');
    expect(after.getAttribute('aria-expanded')).toBe('true');

    act(() => after.click());
    const collapsed = container.querySelector<HTMLElement>('.trace-body')!;
    expect(collapsed.classList.contains('expanded')).toBe(false);
    expect(collapsed.classList.contains('clamped')).toBe(true);
    expect(collapsed.style.maxHeight).toBe(''); // back to the CSS 120px clamp
  });

  it('entry without text renders no body and no toggles', () => {
    mockedScrollHeight = 400;
    renderEntries([entry('')]);
    expect(container.querySelector('.trace-body')).toBeNull();
    expect(container.querySelector('.trace-show-toggle')).toBeNull();
  });
});
