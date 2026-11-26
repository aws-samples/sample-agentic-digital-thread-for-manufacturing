// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The rule-line and leaf-block checks in src/lib/architecture-util.js were
 * patterns with a repeated group (`(?:[-=*_]\s*){3,}`, `(?:-[ \t]*){3,}`).
 * They are now flat checks with no repeated group. These tests pin the new
 * checks to the patterns they replaced, on fixed lines and on a seeded random
 * corpus, and check that a very long marker line stays fast.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const UTIL_JS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../lib/architecture-util.js',
);

function loadCjs(): Record<string, any> {
  const mod = { exports: {} as Record<string, any> };
  // eslint-disable-next-line no-new-func
  new Function('module', 'exports', readFileSync(UTIL_JS, 'utf-8'))(mod, mod.exports);
  return mod.exports;
}

const m = loadCjs();

const OLD_RULE = /^(?:[-=*_]\s*){3,}$/;
const OLD_LEAF =
  /^ {0,3}(?:#{1,6}(?:\s|$)|`{3}|~{3}|(?:-[ \t]*){3,}$|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$)/;

/** Small seeded generator, so a failure names a line that reproduces. */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

function corpus(alphabet: string, fixed: string[], seed: number): string[] {
  const next = rng(seed);
  const out = [...fixed];
  for (let i = 0; i < 20000; i++) {
    let s = '';
    const n = Math.floor(next() * 12);
    for (let j = 0; j < n; j++) s += alphabet[Math.floor(next() * alphabet.length)];
    out.push(s);
  }
  return out;
}

describe('line classifiers match the patterns they replaced', () => {
  it('_isRuleLine', () => {
    const fixed = ['---', '- - -', '***', '___', '==', '===', '-=*_', '--- ', ' ---', '--', '-\n-\n-', '', 'a---'];
    for (const ln of corpus('-=*_ \t\na', fixed, 1118)) {
      expect([ln, m._isRuleLine(ln)]).toEqual([ln, OLD_RULE.test(ln)]);
    }
  });

  it('_opensLeaf', () => {
    const fixed = [
      '---', '   ---', '    ---', '- - -', '-*-', '* * *', '__ _', '# x', '####### x', '#', '```', '~~~',
      '--', '-- -\t', ' - x', '',
    ];
    for (const ln of corpus('-*_ \t#`~x', fixed, 1119)) {
      expect([ln, m._opensLeaf(ln)]).toEqual([ln, OLD_LEAF.test(ln)]);
    }
  });

  it('stays fast on a very long marker line', () => {
    const line = '- '.repeat(500000) + 'x';
    const start = performance.now();
    m._isRuleLine(line);
    m._opensLeaf(line);
    expect(performance.now() - start).toBeLessThan(500);
  });
});
