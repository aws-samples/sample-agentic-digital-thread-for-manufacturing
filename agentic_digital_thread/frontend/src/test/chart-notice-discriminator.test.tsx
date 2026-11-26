// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The omission notice is classified on an EXPLICIT discriminator
 * (kind="notice"), not on "empty datasets". An ordinary empty chart or a
 * legacy labels+data block keeps its normal chart render/drop path. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ChartIframe } from '../chat/ChartIframe';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOTICE =
  `type='line' kind='notice' title='3 series not charted: no name recorded.' labels='[]' datasets='[]'`;
const EMPTY_CHART = `type='bar' title='Downtime by Shift' labels='[]' datasets='[]'`;
const LEGACY = `type='line' title='Legacy' labels='["a"]' data='[1]'`;

describe('ChartIframe notice discriminator', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('renders a kind="notice" block as accessible note text', () => {
    act(() => root.render(<ChartIframe attrs={NOTICE} />));
    expect(container.querySelector('iframe')).toBeNull();
    const note = container.querySelector('[role="note"]');
    expect(note).not.toBeNull();
    expect(note!.textContent).toContain('3 series not charted: no name recorded.');
  });

  it('renders an ordinary empty chart on its normal path, not as a notice', () => {
    act(() => root.render(<ChartIframe attrs={EMPTY_CHART} />));
    expect(container.querySelector('[role="note"]')).toBeNull();
    // An empty datasets chart renders the iframe (its legacy behaviour).
    expect(container.querySelector('iframe')).not.toBeNull();
  });

  it('renders a legacy labels+data chart on its normal path, not as a notice', () => {
    act(() => root.render(<ChartIframe attrs={LEGACY} />));
    expect(container.querySelector('[role="note"]')).toBeNull();
  });
});
