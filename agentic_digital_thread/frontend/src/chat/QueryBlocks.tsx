// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <QueryBlocks/> — renders an executed-query body as copyable <pre>
 * block(s), used by EvidenceCard's Tier3Loaded and RoutingQueryLoaded
 * (kept OUT of EvidenceCard.tsx — that file sits against the 800-line cap).
 *
 * When splitSqlStatements finds 2+ semicolon-terminated statements, each
 * statement renders as its OWN literal-text <pre> (same className/style as
 * the single-block rendering, so existing selector contracts hold), each
 * independently copyable, plus a "Copy all" button that copies the EXACT
 * untruncated original text. When the text is not splittable (single
 * statement, empty, comment-only), the output is EXACTLY the pre-existing
 * single-block rendering (original text verbatim — no trim, no reflow)
 * plus its copy button: a strict superset of the previous behavior.
 *
 * Sink policy: the query text is UNTRUSTED data — every string here is a
 * React TEXT NODE inside a <pre>; never markdown, never highlighted,
 * never HTML. */

import { useI18n } from '../lib/i18n';
import { CopyablePre, CopyButton } from './CopyButton';
import { splitSqlStatements } from './sql-statements';

/** Fill {i}/{n} placeholders in a translated label fragment (the chat.copy*
 * dictionary entries are parameterized — the i18n layer has no interpolation
 * of its own). Unknown placeholders pass through untouched. */
function fill(template: string, vars: Record<string, number>): string {
  return template.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}

export interface QueryBlocksProps {
  /** The full executed-query text (untrusted data). */
  text: string;
  /** The existing per-block className (e.g. 'evidence-executed-query'). */
  preClassName: string;
  /** Optional <pre> style — applied unchanged to every block. Omit it when
   * the className carries the styling (evidence-card.css). */
  preStyle?: React.CSSProperties;
  /** Copy-button aria-label base, e.g. 'Copy executed query'. */
  copyLabel: string;
}

export function QueryBlocks({ text, preClassName, preStyle, copyLabel }: QueryBlocksProps) {
  const { t } = useI18n();
  const statements = splitSqlStatements(text);
  if (statements.length < 2) {
    return (
      <CopyablePre text={text} className={preClassName} preStyle={preStyle} copyLabel={copyLabel} />
    );
  }
  const n = statements.length;
  return (
    <div className={`${preClassName}-group`}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '0.2rem' }}>
        <CopyButton text={text} label={`${copyLabel} — ${fill(t('chat.copyAllStatements'), { n })}`} />
      </div>
      {statements.map((stmt, i) => (
        <CopyablePre
          key={`stmt-${i}`}
          text={stmt}
          className={preClassName}
          preStyle={preStyle}
          copyLabel={`${copyLabel} — ${fill(t('chat.copyStatementOf'), { i: i + 1, n })}`}
        />
      ))}
    </div>
  );
}
