// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ChartIframe/> — the sandboxed Chart.js iframe as a React component
 * (zero-innerHTML migration; was the imperative buildChartIframe/replaceWith
 * pair in charts.ts). srcdoc is a sandboxed DOCUMENT payload (opaque origin,
 * allow-scripts only), not a DOM sink — the template and inline style match
 * the old element byte-for-byte. Unparseable chart-block attributes render
 * nothing (the old path console.warned and left the placeholder). */
import { useMemo } from 'react';

import { buildChartConfigFromAttrs, buildChartSrcdoc, noticeTextFromAttrs } from './charts';

const IFRAME_STYLE: React.CSSProperties = {
  width: '100%',
  height: 320,
  border: '1px solid var(--border)',
  borderRadius: 8,
  margin: '0.75rem 0',
  background: '#0d1117',
};

const NOTICE_STYLE: React.CSSProperties = {
  border: '1px solid var(--border)',
  borderRadius: 8,
  margin: '0.75rem 0',
  padding: '0.6rem 0.8rem',
  fontSize: '0.85rem',
  color: 'var(--muted, #8b949e)',
};

export interface ChartIframeProps {
  /** The raw attribute string captured from the `<chart-block .../>` tag. */
  attrs: string;
  /** false = the data-modal overwrite-defaults delta. */
  preserveExisting?: boolean;
}

export function ChartIframe({ attrs, preserveExisting = true }: ChartIframeProps) {
  // A kind="notice" block is the SiteWise omission notice, not a chart:
  // render it as accessible note text, never a blank chart iframe. Any other
  // block (including an ordinary empty chart) keeps the chart render path.
  const notice = useMemo(() => noticeTextFromAttrs(attrs), [attrs]);
  const srcdoc = useMemo(() => {
    if (notice != null) return null;
    const config = buildChartConfigFromAttrs(attrs, { preserveExisting });
    return config == null ? null : buildChartSrcdoc(config);
  }, [attrs, preserveExisting, notice]);
  if (notice != null) {
    return (
      <div role="note" className="chart-notice" style={NOTICE_STYLE}>
        {notice}
      </div>
    );
  }
  if (srcdoc == null) return null;
  return (
    <iframe
      className="chart-iframe-enter"
      sandbox="allow-scripts"
      srcDoc={srcdoc}
      style={IFRAME_STYLE}
      title="chart"
    />
  );
}
