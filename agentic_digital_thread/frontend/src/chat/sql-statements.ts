// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* splitSqlStatements — pure tokenizer-style scan that splits a
 * multi-statement SQL body on semicolon-terminated statement boundaries.
 *
 * A naive split(';') corrupts semicolons inside string literals, quoted
 * identifiers, dollar-quoted blocks, and comments. The scan therefore
 * respects:
 *   - single-quoted literals (with SQL-standard '' doubling AND backslash
 *     escapes) and double-quoted identifiers;
 *   - PostgreSQL dollar-quoted blocks ($$ … $$ / $tag$ … $tag$);
 *   - line comments (-- … end of line) and block comments.
 *
 * Conservative by construction: an UNTERMINATED literal/comment/dollar
 * block consumes the rest of the input, so we never split inside one.
 * Statements keep their trailing semicolons and are trimmed; empty
 * fragments (bare semicolons, whitespace) are dropped. Callers treat a
 * result of fewer than 2 statements as "not splittable" and render the
 * original text verbatim. No DOM, no formatting — the text stays untrusted
 * literal data. */

const DOLLAR_TAG_RE = /^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$/;

/** Skip a quoted region opened at `start`. Handles backslash escapes.
 * SQL-standard '' doubling needs no special case: the closing quote of the
 * pair ends the region and the second quote immediately re-opens one, so
 * the text between doubled quotes is protected either way. Returns the
 * index AFTER the closing quote (or end of input when unterminated). */
function skipQuoted(text: string, start: number, quote: string): number {
  let i = start + 1;
  while (i < text.length) {
    const ch = text[i];
    if (ch === '\\') {
      i += 2;
      continue;
    }
    if (ch === quote) {
      return i + 1;
    }
    i += 1;
  }
  return text.length;
}

/** Skip a `-- …` line comment. Returns the index after the newline. */
function skipLineComment(text: string, start: number): number {
  const nl = text.indexOf('\n', start);
  return nl === -1 ? text.length : nl + 1;
}

/** Skip a block comment. Returns the index after the closing marker. */
function skipBlockComment(text: string, start: number): number {
  const end = text.indexOf('*/', start + 2);
  return end === -1 ? text.length : end + 2;
}

/** Skip a dollar-quoted block ($$ … $$ / $tag$ … $tag$) opened at `start`.
 * Returns `start` unchanged when `start` is not a dollar-quote opener
 * (e.g. a positional parameter like $1). */
function skipDollarQuoted(text: string, start: number): number {
  const m = DOLLAR_TAG_RE.exec(text.slice(start));
  if (!m) return start;
  const tag = m[0];
  const close = text.indexOf(tag, start + tag.length);
  return close === -1 ? text.length : close + tag.length;
}

/** Split `text` into trimmed SQL statements (semicolons kept). Fewer than
 * 2 results means "not splittable" — see the module header. */
export function splitSqlStatements(text: string): string[] {
  const out: string[] = [];
  let stmtStart = 0;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "'" || ch === '"') {
      i = skipQuoted(text, i, ch);
      continue;
    }
    if (ch === '-' && text[i + 1] === '-') {
      i = skipLineComment(text, i);
      continue;
    }
    if (ch === '/' && text[i + 1] === '*') {
      i = skipBlockComment(text, i);
      continue;
    }
    if (ch === '$') {
      const end = skipDollarQuoted(text, i);
      if (end > i) {
        i = end;
        continue;
      }
    }
    if (ch === ';') {
      const stmt = text.slice(stmtStart, i + 1).trim();
      if (stmt !== ';' && stmt !== '') out.push(stmt);
      stmtStart = i + 1;
    }
    i += 1;
  }
  const tail = text.slice(stmtStart).trim();
  if (tail !== '') out.push(tail);
  return out;
}
