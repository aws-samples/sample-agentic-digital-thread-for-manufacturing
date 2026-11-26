// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Lint-style guard: no DECORATIVE emojis in the swept UI-source files
 * (emoji-reduction pass, 2026-07-16 — user: emoji use was "generally
 * overdone"). Semantic typographic glyphs stay allowed: status marks
 * (✓ ✗ ○ ◐ ⏱), the ⚠ warning glyph, media-control glyphs (▶ ⏸ ■),
 * arrows and the ✦/✕/↺/↻ chrome marks.
 *
 * Scope: files owned/swept by this pass. NOT scanned (parallel-stage
 * ownership or sanctioned as-is):
 *   - chat/** (ChatSidebar/ChatThread wave) and views/architecture/**;
 *   - shared/thread/html.ts — SYS_ICONS (the _sysIcon table) has ZERO
 *     render consumers (only its unit test), so its
 *     pictographs never reach the UI;
 *   - src/test/** fixtures.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/** Semantic, typographic glyphs that are ALLOWED (not decorative). */
const ALLOWED = new Set([
  '✓', '✗', '○', '◐', '⏱', // status marks
  '⚠', // warning glyph (carries meaning)
  '▶', '⏸', '■', '●', '◌', '▪', '▾', // controls / bullets / state dots
  '✕', '✦', '✎', '↺', '↻', // chrome marks (close, checkpoint, edit, reset, re-run)
  '→', '←', '↑', '↓', '↔', '↗', '↩', '⇒', '⇐', // arrows (typographic)
]);

/** Swept files (relative to src/). */
const SWEPT_FILES = [
  'shared/thread/data-modal.tsx',
  'shared/thread/answer-blocks.tsx',
  'shared/thread/panels.tsx',
  'shared/thread/ThreadPanel.tsx',
  'shared/ui/Md.tsx',
  'shared/agents/cards.tsx',
  'views/dashboard/SystemsStrip.tsx',
  'views/dashboard/LiveFeed.tsx',
  'views/dashboard/demo.ts',
  'views/onboard/chat.ts',
  'views/onboard/suggestions-store.ts',
  'views/onboard/wizard.ts',
  'views/onboard/catalog.ts',
  'views/onboard/OnboardView.tsx',
  'views/onboard/components/ObFlow.tsx',
  'views/onboard/components/Checkpoint.tsx',
  'views/onboard/components/PersonaGroups.tsx',
  'views/onboard/components/ResolveExtras.tsx',
  'views/onboard/components/SystemDetail.tsx',
  'lib/i18n.ts',
  'pages/app/AppShell.tsx',
  'pages/app/useViewRouting.ts',
];

/** Every pictographic char (Unicode Extended_Pictographic) not in ALLOWED
 * is decorative for our purposes. */
const PICTOGRAPHIC = /\p{Extended_Pictographic}/gu;

function decorativeIn(text: string): Map<string, number[]> {
  const hits = new Map<string, number[]>();
  text.split('\n').forEach((line, i) => {
    for (const m of line.matchAll(PICTOGRAPHIC)) {
      if (!ALLOWED.has(m[0])) {
        if (!hits.has(m[0])) hits.set(m[0], []);
        hits.get(m[0])!.push(i + 1);
      }
    }
  });
  return hits;
}

describe('no decorative emoji in swept UI sources', () => {
  it.each(SWEPT_FILES)('%s is free of decorative emoji', (rel) => {
    const path = fileURLToPath(new URL(`../${rel}`, import.meta.url));
    const hits = decorativeIn(readFileSync(path, 'utf-8'));
    const report = [...hits.entries()]
      .map(([ch, lines]) => `${ch} (U+${ch.codePointAt(0)!.toString(16).toUpperCase()}) at line(s) ${lines.join(', ')}`)
      .join('; ');
    expect(report, `decorative emoji found in ${rel}: ${report}`).toBe('');
  });
});
