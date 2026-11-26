// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <QueryBlocks/> pins:
 * - a multi-statement body splits into ONE <pre> per statement (same
 *   className as the single-block rendering), each independently copyable,
 *   plus a copy-all button carrying the EXACT untruncated original text;
 * - a non-splittable body (single statement / whitespace) renders EXACTLY
 *   the single-block output: one <pre>, original text VERBATIM (untrimmed),
 *   no group wrapper — strict superset of the pre-split behavior;
 * - the query text stays a LITERAL text node (untrusted — zero innerHTML). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { QueryBlocks } from '../chat/QueryBlocks';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  writeText = vi.fn((_t: string) => Promise.resolve());
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  Object.defineProperty(window.navigator, 'clipboard', {
    value: undefined,
    configurable: true,
  });
  vi.restoreAllMocks();
});

function render(text: string) {
  act(() => {
    root.render(
      <QueryBlocks
        text={text}
        preClassName="evidence-executed-query"
        preStyle={{ fontSize: '0.65rem' }}
        copyLabel="Copy executed query"
      />,
    );
  });
}

const MULTI =
  "SET search_path TO erp;\nSELECT * FROM orders WHERE note = 'a;b';\nSELECT count(*) FROM order_lines;\nSELECT 1;";

describe('<QueryBlocks/> multi-statement split', () => {
  it('renders one <pre> per statement, each with its own copy button, plus copy-all', async () => {
    render(MULTI);
    const pres = [...container.querySelectorAll('pre.evidence-executed-query')];
    expect(pres.map((p) => p.textContent)).toEqual([
      'SET search_path TO erp;',
      "SELECT * FROM orders WHERE note = 'a;b';",
      'SELECT count(*) FROM order_lines;',
      'SELECT 1;',
    ]);
    // one copy button per statement + one copy-all
    const buttons = [...container.querySelectorAll<HTMLButtonElement>('button.adt-copy-btn')];
    expect(buttons).toHaveLength(5);

    // per-statement button copies ONLY its statement
    await act(async () => {
      buttons[2].click(); // first statement's button (index 0 = copy-all, 1..4 = statements)
    });
    // copy-all copies the EXACT untruncated original
    await act(async () => {
      buttons[0].click();
    });
    expect(writeText.mock.calls.map((c) => c[0])).toEqual([
      "SELECT * FROM orders WHERE note = 'a;b';",
      MULTI,
    ]);
    expect(buttons[0].getAttribute('aria-label')).toContain('all 4 statements');
  });
});

describe('<QueryBlocks/> non-splittable = exact single-block superset', () => {
  it('a single statement renders ONE <pre> with the original text VERBATIM (untrimmed)', () => {
    const single = "  SELECT a FROM t WHERE n = 'x;y';\n"; // leading/trailing whitespace preserved
    render(single);
    const pres = [...container.querySelectorAll('pre.evidence-executed-query')];
    expect(pres).toHaveLength(1);
    expect(pres[0].textContent).toBe(single);
    expect(container.querySelector('.evidence-executed-query-group')).toBeNull();
    // exactly one copy button (no copy-all row)
    expect(container.querySelectorAll('button.adt-copy-btn')).toHaveLength(1);
  });

  it('hostile text renders as a LITERAL text node — never elements', () => {
    const hostile = 'SELECT 1 -- <script>alert(1)</script><img src=x onerror=alert(2)>';
    render(hostile);
    expect(container.querySelector('pre.evidence-executed-query')!.textContent).toBe(hostile);
    expect(container.querySelector('script,img')).toBeNull();
  });
});
