// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Shared markdown component (zero-innerHTML migration).
 *
 * <Md text .../> renders markdown as REACT ELEMENTS via react-markdown +
 * remark-gfm (tables/strikethrough/autolinks/tasklists — GFM parity with the
 * previous marked-based pipeline). There is NO HTML-string sink on this path:
 * react-markdown never parses raw HTML embedded in the markdown (no rehype-raw
 * plugin is installed), so `<script>`/`<img onerror>`/any other tag arrives as
 * inert `html` nodes that are skipped — a deliberate security improvement over
 * the old marked + DOMPurify string pipeline (which allowed benign raw tags
 * like `<b>`/`<br>` through; model-emitted raw HTML no longer renders).
 *
 * `inline` unwraps the paragraph wrapper and renders into a <span> (the old
 * marked.parseInline semantics, used by dashboard briefing titles).
 *
 * `bare` renders the markdown elements WITHOUT the container div — for
 * callers that compose chunks into their own container (answer sections,
 * chart interleaving). className/id/postProcess are unsupported in bare mode.
 *
 * `postProcess` runs as a ref effect over the rendered container after each
 * commit — for idempotent DOM passes over RENDERED output (e.g. the demo
 * script inline-style pass). It is NOT a sanitization hook.
 *
 * Citation badges: the chat answer path pre-transforms `[N]` into markdown
 * links `[N](<#cite-TARGET> "label")` (chat/citations.ts); the `a` component
 * override below renders any `#cite-*` link as the `.cite-badge` anchor with
 * `data-cite-target` — same class/data-attribute contract as before, so the
 * delegated thread click handler (chat/thread-clicks.ts) keeps firing.
 *
 * The component is memoized: during token streaming only the chunk whose text
 * changed re-parses (react-markdown's parse is heavier than marked's — see
 * the perf note in chat/AnswerBody.tsx). */
import { Component, memo, useEffect, useRef, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { remarkLocalTimes } from '../../lib/answer-times';

/** Remark plugins for every surface that renders answer markdown: GFM, and
 * UTC answer times shown in the viewer's zone (lib/answer-times.ts). Shared
 * with the investigation trace prose so both read times the same way. */
export const MD_REMARK_PLUGINS = [remarkGfm, remarkLocalTimes];

/** Href prefix that marks a markdown link as a citation badge. */
export const CITE_HREF_PREFIX = '#cite-';

type AnchorProps = React.AnchorHTMLAttributes<HTMLAnchorElement> & { node?: unknown };

/** Citation ordinal from a `#cite-*` link's text — the citations.ts
 * pre-transform renders `[N]` as a link whose text IS the number, so the
 * badge can carry `data-cite-num` for per-ordinal citation identity in
 * thread-clicks (`data-cite-target` alone is non-unique: two same-system
 * citations share `system-<tool>`). Non-numeric text ⇒ no attribute. */
function citeNumFromChildren(children: ReactNode): string | undefined {
  const text = (Array.isArray(children) ? children : [children])
    .map((c) => (typeof c === 'string' || typeof c === 'number' ? String(c) : ''))
    .join('');
  return /^\d+$/.test(text) ? text : undefined;
}

/** `a` override: `#cite-*` links render as citation badges (contract:
 * `.cite-badge` + `data-cite-target` + `data-cite-num`, handled by the
 * delegated thread click handler); everything else renders as a plain
 * anchor. */
function MdAnchor({ node: _node, href, title, children, ...rest }: AnchorProps) {
  if (href && href.startsWith(CITE_HREF_PREFIX)) {
    let target = href.slice(CITE_HREF_PREFIX.length);
    try {
      target = decodeURIComponent(target);
    } catch {
      /* keep the raw fragment if it is not valid percent-encoding */
    }
    return (
      <a
        href="#"
        className="cite-badge"
        data-cite-target={target}
        data-cite-num={citeNumFromChildren(children)}
        title={title}
      >
        {children}
      </a>
    );
  }
  return (
    // Reviewed (react-props-spreading): react-markdown component-override
    // contract — rest carries the renderer's typed anchor props; href itself
    // is passed explicitly and sanitized by react-markdown's urlTransform.
    // nosemgrep
    <a href={href} title={title} {...rest /* nosemgrep */}>
      {children}
    </a>
  );
}

const BLOCK_COMPONENTS: Components = { a: MdAnchor };

const CITE_LINK_START_RE = /\[\d+\]\((?:<#cite-|#cite-)/g;
const COMPLETE_CITE_LINK_RE =
  /^\[\d+\]\((?:<#cite-[^>\r\n]+>|#cite-[^\s)]+)(?:\s+(?:"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|\((?:\\[\s\S]|[^)\\])*\)))?\s*\)/;
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;
const INLINE_CODE_RE = /(`+)(.+?)\1/g;
const STANDALONE_BR_RE = /<br\s*\/?\s*>/gi;

/** Whether `offset` is protected by a fenced block or an inline code span.
 * This mirrors the markdown-level guards used by the citation injector: raw
 * examples must remain byte-for-byte text rather than becoming UI syntax. */
function isInsideMarkdownCode(md: string, offset: number): boolean {
  const lineStart = md.lastIndexOf('\n', offset - 1) + 1;
  const lineEndAt = md.indexOf('\n', offset);
  const lineEnd = lineEndAt === -1 ? md.length : lineEndAt;
  const currentLine = md.slice(lineStart, lineEnd);

  let fenceMarker = '';
  for (const line of md.slice(0, lineStart).split('\n')) {
    const marker = FENCE_RE.exec(line)?.[1] ?? '';
    if (!marker) continue;
    if (!fenceMarker) fenceMarker = marker[0];
    else if (marker[0] === fenceMarker) fenceMarker = '';
  }
  if (fenceMarker || FENCE_RE.test(currentLine)) return true;

  INLINE_CODE_RE.lastIndex = 0;
  const localOffset = offset - lineStart;
  let match: RegExpExecArray | null;
  while ((match = INLINE_CODE_RE.exec(currentLine)) !== null) {
    if (localOffset >= match.index && localOffset < match.index + match[0].length) return true;
  }
  return false;
}

/** Streaming can stop between the start and closing parenthesis of a citation
 * markdown link. CommonMark correctly treats that tail as plain text, but a
 * per-frame render would flash the implementation markup. Withhold only an
 * unterminated TRAILING cite link; a later frame containing the close parses
 * normally into the badge. Complete links support angle or plain destinations
 * and quoted titles containing escaped quotes and line endings. */
function hideIncompleteTrailingCitation(md: string): string {
  const starts: number[] = [];
  CITE_LINK_START_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CITE_LINK_START_RE.exec(md)) !== null) starts.push(match.index);

  for (let i = starts.length - 1; i >= 0; i--) {
    const start = starts[i];
    if (isInsideMarkdownCode(md, start)) continue;
    if (!COMPLETE_CITE_LINK_RE.test(md.slice(start))) return md.slice(0, start);
    break;
  }
  return md;
}

/** Convert the one intentionally-supported raw HTML spelling into markdown
 * hard breaks. All other HTML remains inert, and code examples are untouched. */
function replaceStandaloneBr(md: string): string {
  let fenceMarker = '';
  return md
    .split('\n')
    .map((line) => {
      const marker = FENCE_RE.exec(line)?.[1] ?? '';
      if (marker) {
        if (!fenceMarker) fenceMarker = marker[0];
        else if (marker[0] === fenceMarker) fenceMarker = '';
        return line;
      }
      if (fenceMarker) return line;

      INLINE_CODE_RE.lastIndex = 0;
      let out = '';
      let last = 0;
      let match: RegExpExecArray | null;
      while ((match = INLINE_CODE_RE.exec(line)) !== null) {
        out += line.slice(last, match.index).replace(STANDALONE_BR_RE, '  \n') + match[0];
        last = match.index + match[0].length;
      }
      return out + line.slice(last).replace(STANDALONE_BR_RE, '  \n');
    })
    .join('\n');
}

function preprocessMarkdown(md: string): string {
  return replaceStandaloneBr(hideIncompleteTrailingCitation(md));
}

/** Inline mode: unwrap the paragraph wrapper (marked.parseInline semantics). */
const INLINE_COMPONENTS: Components = {
  ...BLOCK_COMPONENTS,
  p: ({ children }) => <>{children}</>,
};

/** Degradation guard: if markdown rendering throws (parser or a component
 * override), fall back to the raw source as PLAIN TEXT — the same
 * escaped-text degradation the legacy path had when its markdown
 * vendor was missing. JSX text rendering escapes by construction. */
interface BoundaryProps {
  fallbackText: string;
  children: ReactNode;
}

export class MdErrorBoundary extends Component<BoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidUpdate(prev: BoundaryProps): void {
    // New text gets a fresh render attempt.
    if (prev.fallbackText !== this.props.fallbackText && this.state.failed) {
      this.setState({ failed: false });
    }
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallbackText : this.props.children;
  }
}

export interface MdProps {
  /** Markdown source (LLM output, briefing text, …). Nullish renders empty. */
  text: unknown;
  /** Unwrap the block <p> wrapper and render into a <span>. */
  inline?: boolean;
  /** Render the markdown elements without a container (caller composes). */
  bare?: boolean;
  /** Ref effect over the rendered container after each commit (idempotent
   * DOM passes over rendered output). Ignored in bare mode. */
  postProcess?: (el: HTMLElement) => void;
  className?: string;
  id?: string;
}

function MdImpl({ text, inline = false, bare = false, postProcess, className, id }: MdProps) {
  const elRef = useRef<HTMLElement | null>(null);
  const setEl = (el: HTMLElement | null) => {
    elRef.current = el;
  };

  const source = text == null ? '' : String(text);
  const s = preprocessMarkdown(source);

  useEffect(() => {
    if (postProcess && elRef.current) postProcess(elRef.current);
  }, [s, postProcess]);

  const rendered = (
    <MdErrorBoundary fallbackText={s}>
      <ReactMarkdown remarkPlugins={MD_REMARK_PLUGINS} components={inline ? INLINE_COMPONENTS : BLOCK_COMPONENTS}>
        {s}
      </ReactMarkdown>
    </MdErrorBoundary>
  );

  if (bare) return rendered;
  return inline ? (
    <span ref={setEl} className={className} id={id}>
      {rendered}
    </span>
  ) : (
    <div ref={setEl} className={className} id={id}>
      {rendered}
    </div>
  );
}

/** Memoized: unchanged chunks never re-parse during streaming re-renders. */
export const Md = memo(MdImpl);
