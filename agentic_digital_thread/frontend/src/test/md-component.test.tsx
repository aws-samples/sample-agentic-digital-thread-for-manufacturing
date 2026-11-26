// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the shared <Md/> component (zero-innerHTML migration): markdown
 * renders as REACT ELEMENTS via react-markdown + remark-gfm — no HTML-string
 * sink, no sanitizer needed. Covers GFM feature parity with the previous
 * marked pipeline (tables/strikethrough/autolinks/lists/code, cases mined
 * from real answers), the `#cite-*` citation-badge anchor override, raw-HTML
 * inertness (XSS), inline/bare modes, and the plain-text degradation
 * boundary. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Md, MdErrorBoundary } from '../shared/ui/Md';

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
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

describe('<Md/> markdown element rendering', () => {
  it('renders markdown emphasis into a div', () => {
    render(<Md text="This is **important**." />);
    const div = container.querySelector('div');
    expect(div).not.toBeNull();
    expect(div!.querySelector('strong')?.textContent).toBe('important');
  });

  it('renders headings, lists and inline code (real-answer shape)', () => {
    const md =
      '## Production Summary\n\n- Line 2 at `92%` OEE\n- Filler bottleneck on shift 3\n\n1. Check CMMS\n2. Schedule PM';
    render(<Md text={md} />);
    expect(container.querySelector('h2')?.textContent).toBe('Production Summary');
    const items = [...container.querySelectorAll('ul li')];
    expect(items).toHaveLength(2);
    expect(items[0].querySelector('code')?.textContent).toBe('92%');
    expect([...container.querySelectorAll('ol li')].map((li) => li.textContent)).toEqual([
      'Check CMMS',
      'Schedule PM',
    ]);
  });

  it('renders GFM tables (parity with the marked pipeline)', () => {
    const md = '| Line | OEE |\n|------|-----|\n| 2 | 92% |\n| 3 | 88% |';
    render(<Md text={md} />);
    const table = container.querySelector('table')!;
    expect(table).not.toBeNull();
    expect([...table.querySelectorAll('th')].map((th) => th.textContent)).toEqual(['Line', 'OEE']);
    expect([...table.querySelectorAll('tbody td')].map((td) => td.textContent)).toEqual([
      '2',
      '92%',
      '3',
      '88%',
    ]);
  });

  it('renders GFM strikethrough and autolinks', () => {
    render(<Md text={'~~old value~~ see https://example.com/docs now'} />);
    expect(container.querySelector('del')?.textContent).toBe('old value');
    const a = container.querySelector('a')!;
    expect(a.getAttribute('href')).toBe('https://example.com/docs');
  });

  it('renders fenced code blocks', () => {
    render(<Md text={'```sql\nSELECT * FROM orders\n```'} />);
    const code = container.querySelector('pre code')!;
    expect(code.textContent).toContain('SELECT * FROM orders');
  });

  it('renders nullish text as empty', () => {
    render(<Md text={null} />);
    expect(container.querySelector('div')!.textContent).toBe('');
  });

  it('inline mode renders into a span without a <p> wrapper', () => {
    render(<Md text="Line is **up**." inline />);
    const span = container.querySelector('span');
    expect(span).not.toBeNull();
    expect(span!.querySelector('p')).toBeNull();
    expect(span!.querySelector('strong')?.textContent).toBe('up');
  });

  it('bare mode renders elements without a container', () => {
    render(
      <section>
        <Md bare text="**bold** body" />
      </section>,
    );
    // The paragraph is a DIRECT child of the caller's container.
    const sectionEl = container.querySelector('section')!;
    expect(sectionEl.firstElementChild?.tagName).toBe('P');
  });

  it('passes className and id through', () => {
    render(<Md text="x" className="md-body" id="answer-1" />);
    const div = container.querySelector('div#answer-1');
    expect(div).not.toBeNull();
    expect(div!.className).toBe('md-body');
  });

  it('invokes postProcess with the rendered container after paint', () => {
    const seen: string[] = [];
    const postProcess = (el: HTMLElement) => {
      seen.push(el.textContent || '');
    };
    render(<Md text="Hello **world**" postProcess={postProcess} />);
    expect(seen).toHaveLength(1);
    expect(seen[0]).toContain('world');
  });

  it('re-runs postProcess when the text changes', () => {
    const postProcess = vi.fn();
    render(<Md text="one" postProcess={postProcess} />);
    render(<Md text="two" postProcess={postProcess} />);
    expect(postProcess).toHaveBeenCalledTimes(2);
  });
});

describe('<Md/> raw HTML is inert (XSS)', () => {
  it('never renders script tags (block)', () => {
    render(<Md text={'Safe text.\n\n<script>window.__pwned = true;</script>\n\nMore.'} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.innerHTML).not.toContain('<script');
    expect(container.textContent).toContain('Safe text.');
    expect((window as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it('never renders script tags (inline mode)', () => {
    render(<Md text={'hi <script>alert(1)</script>'} inline />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.innerHTML).not.toContain('<script');
  });

  it('never renders event-handler attributes (raw tag stays literal text)', () => {
    render(<Md text={'An image: <img src="x" onerror="alert(1)"> done.'} />);
    expect(container.querySelector('img')).toBeNull();
    // No element carries the handler — the raw tag renders as escaped TEXT
    // (react-markdown without rehype-raw), never as markup.
    expect(container.querySelector('[onerror]')).toBeNull();
    expect(container.innerHTML).toContain('&lt;img');
    expect(container.textContent).toContain('<img src="x" onerror="alert(1)">');
  });

  it('renders benign raw HTML tags as literal text (deliberate change vs marked)', () => {
    // Raw HTML remains inert. The markdown preprocessor has one narrow
    // exception for standalone <br> tags, covered below.
    render(<Md text={'a <b>bold-ish</b> word'} />);
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('bold-ish');
  });

  it('converts standalone br variants into markdown hard line breaks', () => {
    render(<Md text={'LOT-A (Cola)<br>LOT-B (Lemon)<BR/>LOT-C<br />LOT-D'} />);
    expect(container.querySelectorAll('br')).toHaveLength(3);
    expect(container.textContent?.split(/\s*\n\s*/)).toEqual([
      'LOT-A (Cola)',
      'LOT-B (Lemon)',
      'LOT-C',
      'LOT-D',
    ]);
    expect(container.textContent).not.toContain('<br');
  });

  it('does not convert br tags inside inline or fenced code', () => {
    render(<Md text={'Outside<br>next and `<br/>`\n\n```html\n<br />\n```'} />);
    expect(container.querySelectorAll('br')).toHaveLength(1);
    expect(container.querySelector('code')?.textContent).toBe('<br/>');
    expect(container.querySelector('pre code')?.textContent).toContain('<br />');
  });

  it('markdown javascript: links are sanitized by the default url transform', () => {
    render(<Md text={'[click](javascript:alert(1))'} />);
    const a = container.querySelector('a')!;
    expect(a.getAttribute('href') || '').not.toContain('javascript:');
  });
});

describe('<Md/> citation-badge anchor override', () => {
  it('renders #cite-* links as .cite-badge anchors with data-cite-target', () => {
    render(<Md text={'OEE is 92% [1](<#cite-finding-7> "MES Milwaukee")'} />);
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.getAttribute('href')).toBe('#');
    expect(badge.dataset.citeTarget).toBe('finding-7');
    expect(badge.title).toBe('MES Milwaukee');
    expect(badge.textContent).toBe('1');
  });

  it('renders angle-bracket citation destinations with exotic multiline titles', () => {
    render(
      <Md
        text={'Cedar Rapids — Quality Failures 1[4](<#cite-finding-18> "MES — Quality \\"quoted\\"\nCedar Rapids")'}
      />,
    );
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.dataset.citeTarget).toBe('finding-18');
    expect(badge.title).toBe('MES — Quality "quoted"\nCedar Rapids');
    expect(container.textContent).not.toContain('[4](<#cite-');
  });

  it('withholds an incomplete trailing citation until a later stream frame completes it', () => {
    render(<Md text={'Cedar Rapids — Quality Failures Active Today 1[4](<#cite-finding-18> "MES'} />);
    expect(container.querySelector('a.cite-badge')).toBeNull();
    expect(container.textContent).toBe('Cedar Rapids — Quality Failures Active Today 1');
    expect(container.textContent).not.toContain('[4](<#cite-');

    render(
      <Md
        text={'Cedar Rapids — Quality Failures Active Today 1[4](<#cite-finding-18> "MES — Quality")'}
      />,
    );
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.dataset.citeTarget).toBe('finding-18');
    expect(badge.textContent).toBe('4');
    expect(container.textContent).not.toContain('[4](<#cite-');
  });

  it('renders plain #cite-* destinations as badges too', () => {
    render(<Md text={'[5](#cite-finding-19 "ERP — Orders")'} />);
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.dataset.citeTarget).toBe('finding-19');
    expect(badge.title).toBe('ERP — Orders');
  });

  it('percent-decodes the cite target from the href', () => {
    render(<Md text={`[2](<#cite-${encodeURIComponent('system-erp/x y')}>)`} />);
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge.dataset.citeTarget).toBe('system-erp/x y');
  });

  it('leaves ordinary links as plain anchors', () => {
    render(<Md text={'[docs](https://example.com)'} />);
    const a = container.querySelector('a')!;
    expect(a.classList.contains('cite-badge')).toBe(false);
    expect(a.getAttribute('href')).toBe('https://example.com');
  });
});

describe('<MdErrorBoundary/> plain-text degradation', () => {
  function Bomb(): React.ReactElement {
    throw new Error('render exploded');
  }

  it('falls back to the raw source as plain text when rendering throws', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <MdErrorBoundary fallbackText={'**bold** & <b>tag</b>'}>
        <Bomb />
      </MdErrorBoundary>,
    );
    spy.mockRestore();
    // Escaped-text path: no markdown rendering, no live elements — the raw
    // source is shown as text (JSX text is escaped by construction).
    expect(container.querySelector('strong')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('**bold** & <b>tag</b>');
  });
});
