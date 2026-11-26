// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = path.resolve(TEST_DIR, '../..');
const ENTRIES = ['index.html', 'login.html', 'admin.html'] as const;

function viewportTags(source: string): string[] {
  return source.match(/<meta\b[^>]*\bname=["']viewport["'][^>]*>/gi) ?? [];
}

describe('HTML viewport metadata', () => {
  it.each(ENTRIES)('%s retains one width-aware viewport declaration', (entry) => {
    const source = readFileSync(path.join(FRONTEND_ROOT, entry), 'utf8');
    const tags = viewportTags(source);

    expect(tags).toHaveLength(1);
    expect(tags[0]).toMatch(/\bcontent=["'][^"']*width=device-width[^"']*["']/i);
    expect(tags[0]).toMatch(/\bcontent=["'][^"']*initial-scale=1\.0[^"']*["']/i);
  });
});
