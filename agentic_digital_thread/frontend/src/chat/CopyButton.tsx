// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <CopyButton/> — small reusable copy-to-clipboard button for the
 * technical-detail <pre> blocks (evidence executed query, routing query,
 * investigation tool input/output).
 *
 * Copy path: navigator.clipboard.writeText first; when the async Clipboard
 * API is unavailable or rejects, a hidden-textarea document.execCommand
 * fallback runs. The result is HONEST: the button shows "Copied" only when
 * one of the two paths reported success, and "Copy failed" otherwise —
 * it never throws and never pretends success. The confirmation reverts to
 * "Copy" after a short delay. All three state texts resolve through the
 * chrome i18n dictionary (chat.copy / chat.copied / chat.copyFailed);
 * call sites pass already-translated aria-labels (chat.copy* keys).
 *
 * Accessibility: a real <button type="button"> (keyboard reachable), an
 * explicit aria-label, and a visible :focus-visible outline
 * (.adt-copy-btn in styles/enhancements.css).
 *
 * <CopyablePre/> renders a literal-text <pre> (untrusted data — text node
 * only, zero innerHTML) with a CopyButton overlaid top-right; it copies the
 * EXACT untruncated text it renders. */

import { useEffect, useRef, useState } from 'react';

import { useI18n } from '../lib/i18n';

/** Hidden-textarea document.execCommand('copy') fallback. Returns whether
 * the copy actually succeeded; never throws. */
function fallbackCopy(text: string): boolean {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok === true;
  } catch {
    return false;
  }
}

/** Copy `text` to the clipboard. Clipboard API first, execCommand textarea
 * fallback second. Resolves true only on a REPORTED success — never
 * throws, never silently pretends. */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
  if (clip && typeof clip.writeText === 'function') {
    try {
      await clip.writeText(text);
      return true;
    } catch {
      /* fall through to the execCommand path */
    }
  }
  return fallbackCopy(text);
}

const BTN_STYLE: React.CSSProperties = {
  background: 'var(--surface, transparent)',
  border: '1px solid var(--border)',
  borderRadius: 3,
  color: 'var(--muted)',
  cursor: 'pointer',
  fontSize: '0.6rem',
  lineHeight: 1.5,
  padding: '0 0.35rem',
};

export interface CopyButtonProps {
  /** The EXACT text to copy (untruncated). */
  text: string;
  /** aria-label, e.g. "Copy executed query". */
  label: string;
  /** Optional style overrides (e.g. absolute positioning inside a block). */
  style?: React.CSSProperties;
}

type CopyState = 'idle' | 'copied' | 'failed';

/** Dictionary keys per state — resolved through t() so the button follows
 * the chrome language (chat.copy* entries exist in all six locales; the
 * `en` values are byte-identical to the pre-i18n hardcoded strings). */
const STATE_KEY: Record<CopyState, string> = {
  idle: 'chat.copy',
  copied: 'chat.copied',
  failed: 'chat.copyFailed',
};

export function CopyButton({ text, label, style }: CopyButtonProps) {
  const { t } = useI18n();
  const [state, setState] = useState<CopyState>('idle');
  const timerRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  const onClick = async () => {
    const ok = await copyTextToClipboard(text);
    setState(ok ? 'copied' : 'failed');
    window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setState('idle'), 1500);
  };

  return (
    <button
      type="button"
      className="adt-copy-btn"
      aria-label={label}
      aria-live="polite"
      onClick={onClick}
      style={{ ...BTN_STYLE, ...style }}
    >
      {t(STATE_KEY[state])}
    </button>
  );
}

export interface CopyablePreProps {
  /** The block's full text — rendered as a LITERAL text node and copied
   * verbatim. */
  text: string;
  /** The existing block className (test/selector contract — unchanged). */
  className: string;
  /** Optional block <pre> style (omit when the className carries it). */
  preStyle?: React.CSSProperties;
  /** CopyButton aria-label. */
  copyLabel: string;
}

/** A literal-text <pre> with an unobtrusive top-right CopyButton. The <pre>
 * keeps its caller-supplied className and style byte-identical to the
 * pre-CopyButton rendering (the wrapper <div> is margin-transparent). */
export function CopyablePre({ text, className, preStyle, copyLabel }: CopyablePreProps) {
  return (
    <div style={{ position: 'relative' }}>
      <pre className={className} style={preStyle}>
        {text}
      </pre>
      <CopyButton
        text={text}
        label={copyLabel}
        style={{ position: 'absolute', right: '0.3rem', top: '0.45rem' }}
      />
    </div>
  );
}
