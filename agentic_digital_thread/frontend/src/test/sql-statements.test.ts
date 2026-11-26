// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* splitSqlStatements — tokenizer pins. The splitter must respect string
 * literals (incl. doubled-quote and backslash escapes), quoted identifiers,
 * dollar-quoted blocks, -- line comments and block comments; a naive
 * split(';') fails every one of these. Fewer than 2 results = "not
 * splittable" (QueryBlocks renders the original text verbatim). */
import { describe, expect, it } from 'vitest';

import { splitSqlStatements } from '../chat/sql-statements';

describe('splitSqlStatements', () => {
  it('splits two plain statements on the semicolon boundary', () => {
    expect(splitSqlStatements('SELECT 1;\nSELECT 2;')).toEqual(['SELECT 1;', 'SELECT 2;']);
  });

  it('does not split on a semicolon inside a single-quoted literal', () => {
    const sql = "SELECT 'a;b' FROM t; SELECT 2;";
    expect(splitSqlStatements(sql)).toEqual(["SELECT 'a;b' FROM t;", 'SELECT 2;']);
  });

  it('handles a doubled (escaped) quote inside a literal', () => {
    // 'it''s; fine' is ONE literal containing an apostrophe and a semicolon.
    const sql = "SELECT 'it''s; fine' FROM t; SELECT 2;";
    expect(splitSqlStatements(sql)).toEqual(["SELECT 'it''s; fine' FROM t;", 'SELECT 2;']);
  });

  it('handles a backslash-escaped quote inside a literal', () => {
    const sql = "SELECT 'a\\'; b' FROM t; SELECT 2;";
    expect(splitSqlStatements(sql)).toEqual(["SELECT 'a\\'; b' FROM t;", 'SELECT 2;']);
  });

  it('does not split on a semicolon inside a -- line comment', () => {
    const sql = 'SELECT 1 -- note; not a boundary\nFROM t; SELECT 2;';
    expect(splitSqlStatements(sql)).toEqual([
      'SELECT 1 -- note; not a boundary\nFROM t;',
      'SELECT 2;',
    ]);
  });

  it('does not split on a semicolon inside a /* */ block comment', () => {
    const sql = 'SELECT 1 /* note; still\nnot a boundary */ FROM t; SELECT 2;';
    expect(splitSqlStatements(sql)).toEqual([
      'SELECT 1 /* note; still\nnot a boundary */ FROM t;',
      'SELECT 2;',
    ]);
  });

  it('does not split on a semicolon inside a dollar-quoted block', () => {
    const sql = 'CREATE FUNCTION f() AS $body$ SELECT 1; SELECT 2; $body$; SELECT 3;';
    expect(splitSqlStatements(sql)).toEqual([
      'CREATE FUNCTION f() AS $body$ SELECT 1; SELECT 2; $body$;',
      'SELECT 3;',
    ]);
  });

  it('does not split on a semicolon inside a double-quoted identifier', () => {
    const sql = 'SELECT "col;umn" FROM t; SELECT 2;';
    expect(splitSqlStatements(sql)).toEqual(['SELECT "col;umn" FROM t;', 'SELECT 2;']);
  });

  it('a trailing semicolon does not create an empty extra statement', () => {
    expect(splitSqlStatements('SELECT 1; SELECT 2;')).toEqual(['SELECT 1;', 'SELECT 2;']);
    expect(splitSqlStatements('SELECT 1; SELECT 2;  \n')).toEqual(['SELECT 1;', 'SELECT 2;']);
  });

  it('no trailing semicolon keeps the final statement', () => {
    expect(splitSqlStatements('SELECT 1; SELECT 2')).toEqual(['SELECT 1;', 'SELECT 2']);
  });

  it('a single statement stays a single statement (text preserved)', () => {
    expect(splitSqlStatements('SELECT a, b FROM t WHERE x = 1;')).toEqual([
      'SELECT a, b FROM t WHERE x = 1;',
    ]);
    expect(splitSqlStatements('SELECT a FROM t')).toEqual(['SELECT a FROM t']);
  });

  it('empty and whitespace-only input return no statements', () => {
    expect(splitSqlStatements('')).toEqual([]);
    expect(splitSqlStatements('   \n\t ')).toEqual([]);
  });

  it('bare semicolons produce no empty statements', () => {
    expect(splitSqlStatements(';;;')).toEqual([]);
    expect(splitSqlStatements('SELECT 1;;')).toEqual(['SELECT 1;']);
  });

  it('an unterminated literal never splits (conservative)', () => {
    expect(splitSqlStatements("SELECT 'oops; SELECT 2;")).toEqual([
      "SELECT 'oops; SELECT 2;",
    ]);
  });

  it('splits a realistic four-statement body', () => {
    const sql =
      "SET search_path TO erp;\nSELECT * FROM orders WHERE note = 'a;b';\n-- check; totals\nSELECT count(*) FROM order_lines;\nSELECT 1";
    expect(splitSqlStatements(sql)).toHaveLength(4);
  });
});
