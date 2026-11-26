// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, expect, it } from 'vitest';

describe('CHART_TAG_RE quote-awareness (heading-artifact fix)', () => {
  it('does not terminate at > inside a quoted attr value', async () => {
    const { splitChartBlocks } = await import('../chat/charts');
    const md = 'Before\n<chart-block data-title=\'Output ("Milwaukee")\' data-config=\'{"label":"508 -> 33 failed"}\'/>\nAfter';
    const parts = splitChartBlocks(md);
    const mdText = parts.filter((p) => p.kind === 'md').map((p) => (p as {text:string}).text).join('');
    expect(mdText).not.toContain('Milwaukee")');
    expect(mdText).not.toContain('->');
    expect(parts.some((p) => p.kind === 'chart')).toBe(true);
    const chart = parts.find((p) => p.kind === 'chart') as {attrs:string};
    expect(chart.attrs).toContain('508 -> 33 failed');
  });

  it('streaming strip hides a partial tag containing > in quoted values', async () => {
    const { stripChartBlocksForStream } = await import('../chat/charts');
    const streaming = 'Answer text.\n<chart-block data-config=\'{"x":"a -> b"}\' data-tit';
    expect(stripChartBlocksForStream(streaming)).toBe('Answer text.\n');
  });

  it('plain tags still parse exactly as before', async () => {
    const { splitChartBlocks } = await import('../chat/charts');
    const parts = splitChartBlocks('A\n<chart-block data-x="1"/>\nB');
    expect(parts.map((p) => p.kind)).toEqual(['md', 'chart', 'md']);
  });
});
