// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chart-block handling (zero-innerHTML migration).
 *
 * The old pipeline extracted `<chart-block .../>` tags into placeholder divs
 * before markdown parsing and re-injected iframes into the rendered DOM.
 * Markdown now renders as React elements, so the pipeline is string-level:
 * `splitChartBlocks` splits the markdown on chart tags and the caller
 * interleaves <Md/> chunks with <ChartIframe/> components (ChartIframe.tsx).
 *
 * The sandboxed srcdoc template is unchanged: it loads Chart.js by ABSOLUTE
 * URL (`${origin}/js/vendor/chart.umd.min.js`) — the sandboxed iframe has an
 * opaque origin, so the vendored UMD stays a plain public asset (the e2e
 * chart probe depends on it). srcdoc is a sandboxed DOCUMENT payload, not a
 * DOM sink. */

import { localChartLabels, localizeUtcText } from '../lib/answer-times';

const COLORS = ['#00d8ff', '#3fb950', '#d29922', '#f78166', '#bc8cff', '#58a6ff'];

/* Quote-AWARE tag match: attr values are quoted JSON that can legally contain
 * `>` (arrow prose like "508 -> 33", comparators, cypher). The old `[^>]*`
 * terminated at the FIRST `>`, cutting the tag early and leaking the rest of
 * the attribute string into the visible answer (the `Milwaukee")` artifact).
 * Segments: unquoted runs without >/quotes, 'single-quoted', "double-quoted". */
const CHART_TAG_RE = /<chart-block((?:[^>'"]|'[^']*'|"[^"]*")*)\/?>/g;

type Dataset = Record<string, unknown> & { data?: unknown[] };

/** Dataset default fills — shared by the chat path (preserveExisting=true:
 * keeps caller-provided backgroundColor/tension/pointRadius) and the
 * data-modal path (preserveExisting=false: OVERWRITES those defaults
 * unconditionally — a legacy delta preserved 1:1). */
function applyDatasetDefaults(
  datasets: Dataset[],
  spanGaps: boolean,
  preserveExisting = true,
): void {
  datasets.forEach((ds, i) => {
    ds.borderColor = ds.borderColor || COLORS[i % COLORS.length];
    if (preserveExisting) {
      ds.backgroundColor = ds.backgroundColor || ((ds.borderColor as string) + '22');
      ds.tension = ds.tension ?? 0.3;
      ds.pointRadius = ds.pointRadius ?? 2;
      ds.borderWidth = ds.borderWidth ?? 2;
      ds.fill = ds.fill ?? false;
    } else {
      ds.backgroundColor = ((ds.borderColor as string) || COLORS[0]) + '22';
      ds.tension = 0.3;
      ds.pointRadius = 2;
      ds.borderWidth = 2;
      ds.fill = false;
    }
    if (spanGaps) ds.spanGaps = true;
  });
}

/** One markdown/chart part of a split answer. */
export type ChartSplitPart = { kind: 'md'; text: string } | { kind: 'chart'; attrs: string };

/** Split markdown on `<chart-block .../>` tags (same regex the old
 * placeholder extraction used) into markdown chunks interleaved with the
 * captured attribute strings. Empty markdown chunks are dropped. */
export function splitChartBlocks(text: string): ChartSplitPart[] {
  const parts: ChartSplitPart[] = [];
  let last = 0;
  CHART_TAG_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = CHART_TAG_RE.exec(text)) !== null) {
    const before = text.slice(last, m.index);
    if (before.trim()) parts.push({ kind: 'md', text: before });
    parts.push({ kind: 'chart', attrs: m[1] });
    last = m.index + m[0].length;
  }
  const tail = text.slice(last);
  if (tail.trim()) parts.push({ kind: 'md', text: tail });
  return parts;
}

/** Remove chart-block tags (Word export — the old export path left invisible
 * placeholder divs; the export stylesheet hid iframes anyway). */
export function stripChartBlocks(text: string): string {
  return text.replace(CHART_TAG_RE, '');
}

/** Streaming variant: also removes a TRAILING un-terminated `<chart-block`
 * open tag still being streamed (the old DOMPurify streaming
 * path never showed chart-tag source; complete tags strip via
 * stripChartBlocks, and the partial tail is hidden until it completes). */
export function stripChartBlocksForStream(text: string): string {
  // Same quote-aware segment grammar as CHART_TAG_RE for the un-terminated
  // trailing tag: a `>` inside a quoted attr value must not stop the strip
  // (or raw tag source flashes during streaming).
  return stripChartBlocks(text).replace(/<chart-block(?:[^>'"]|'[^']*'|"[^"]*")*(?:'[^']*|"[^"]*)?$/, '');
}

/** The omission notice the SiteWise history path emits
 * (system_agent._sitewise_notice_tag) carries an EXPLICIT kind="notice"
 * discriminator. Classify a block as a notice ONLY on that attribute, never on
 * "empty datasets": an ordinary empty chart or a legacy labels+data block has
 * no kind and keeps its normal chart render/drop path. Returns the notice text
 * (the block's title) when the block is a notice, else null (a real chart). */
export function noticeTextFromAttrs(attrs: string): string | null {
  const attrRe = /(\w[\w-]*)=(?:'([^']*)'|"([^"]*)")/g;
  let m: RegExpExecArray | null;
  const parsed: Record<string, string> = {};
  while ((m = attrRe.exec(attrs)) !== null) {
    parsed[m[1]] = m[2] !== undefined ? m[2] : m[3];
  }
  if (parsed.kind !== 'notice') return null;
  return (parsed.title || '').trim() || null;
}

/** Build the Chart.js config from a chart-block's raw attribute string —
 * the old placeholder-reinjection parse, minus the DOM. Returns null (after
 * a console.warn) when the attributes do not parse. */
export function buildChartConfigFromAttrs(
  attrs: string,
  opts?: { preserveExisting?: boolean },
): unknown | null {
  // Parse attributes — values may be single-quoted and contain double quotes (JSON)
  const attrRe = /(\w[\w-]*)=(?:'([^']*)'|"([^"]*)")/g;
  let m: RegExpExecArray | null;
  const parsed: Record<string, string> = {};
  while ((m = attrRe.exec(attrs)) !== null) {
    parsed[m[1]] = m[2] !== undefined ? m[2] : m[3];
  }
  try {
    const type = parsed.type || 'line';
    const title = localizeUtcText(parsed.title || '');
    const rawLabels = JSON.parse(parsed.labels || '[]');
    const datasets = JSON.parse(parsed.datasets || '[]') as Dataset[];
    applyDatasetDefaults(datasets, false, opts?.preserveExisting ?? true);
    // UTC instant labels show in the viewer's zone, like the answer prose:
    // the data carries the full local time (the tooltip), tickLabels the short
    // axis text (installed as the tick callback inside the chart document).
    const times = Array.isArray(rawLabels) ? localChartLabels(rawLabels) : null;
    const labels = times ? times.full : rawLabels;
    return {
      type,
      data: { labels, datasets },
      ...(times ? { tickLabels: times.ticks } : {}),
      options: {
        responsive: true,
        animation: false,
        plugins: {
          title: { display: !!title, text: title, color: '#e6edf3', font: { size: 13 } },
          legend: { labels: { color: '#e6edf3', font: { size: 11 } } },
        },
        scales: {
          x: {
            ticks: { color: '#8b949e', maxRotation: 45, font: { size: 10 } },
            grid: { color: '#21262d' },
          },
          y: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } },
        },
      },
    };
  } catch (e) {
    console.warn('Chart inject failed:', e, attrs.substring(0, 100));
    return null;
  }
}

/** The sandboxed chart-iframe document for a Chart.js config — exact srcdoc
 * template (absolute vendor URL included). Rendered by <ChartIframe/>.
 * The config JSON escapes `<` as \u003c so a model-authored label containing
 * `</script>` cannot close the script context (review hardening; the iframe
 * is opaque-origin sandboxed either way). A config carrying `tickLabels`
 * (UTC instant labels, see buildChartConfigFromAttrs) gets them as the x-axis
 * tick callback, since a function cannot travel inside JSON. */
export function buildChartSrcdoc(chartConfig: unknown): string {
  const configJson = JSON.stringify(chartConfig).replace(/</g, '\\u003c');
  // Security-reviewed (html-in-template-string): the only interpolations are
  // window.location.origin and JSON.stringify output with `<` escaped to
  // \u003c, and the document renders exclusively in an opaque-origin
  // sandboxed iframe (see ChartIframe). Not a live-DOM sink.
  // nosemgrep
  return `<!DOCTYPE html><html><head>
    <script src="${window.location.origin}/js/vendor/chart.umd.min.js"><\/script>
    <style>body{margin:0;background:#0d1117;display:flex;align-items:center;justify-content:center;height:100vh;}canvas{max-width:100%;}</style>
    </head><body>
    <canvas id="c"></canvas>
    <script>var cfg=${configJson};var t=cfg.tickLabels;delete cfg.tickLabels;if(t){cfg.options.scales.x.ticks.callback=function(v){return t[v];};}new Chart(document.getElementById('c'),cfg);<\/script>
    </body></html>`;
}
