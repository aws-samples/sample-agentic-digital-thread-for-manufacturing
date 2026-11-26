// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ChartIframe/> motion-polish contract: the iframe carries the mount-fade
 * class (styled in enhancements.css) while the security-relevant attributes
 * stay untouched — sandbox="allow-scripts" and a srcdoc payload (opaque
 * origin, never a DOM sink). */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ChartIframe } from '../chat/ChartIframe';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ATTRS = `type='line' labels='["a","b"]' datasets='[{"label":"x","data":[1,2]}]'`;

describe('ChartIframe', () => {
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

  it('renders the enter-motion class with the sandbox contract intact', () => {
    act(() => root.render(<ChartIframe attrs={ATTRS} />));
    const iframe = container.querySelector('iframe');
    expect(iframe).not.toBeNull();
    expect(iframe!.className).toBe('chart-iframe-enter');
    expect(iframe!.getAttribute('sandbox')).toBe('allow-scripts');
    expect(iframe!.getAttribute('srcdoc')).toContain('<!DOCTYPE html>');
  });

  it('renders nothing for unparseable chart attributes', () => {
    act(() => root.render(<ChartIframe attrs="type='line' labels='[broken]'" />));
    expect(container.querySelector('iframe')).toBeNull();
  });
});
