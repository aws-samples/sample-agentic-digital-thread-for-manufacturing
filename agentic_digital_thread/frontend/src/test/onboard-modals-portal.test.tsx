// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The onboard overlays render on <body>, not inside the onboard view.
 *
 * The view keeps the view-enter animation's transform (fill-mode both), which
 * makes it a stacking context: an overlay rendered inside it sat under the
 * open chat sidebar whatever its z-index, so Remove system looked dead while
 * the chat was open. ObModals portals to <body> to escape that context. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ObModals } from '../views/onboard/components/ObModals';
import { closeModal, openModal } from '../views/onboard/flow-store';

let view: HTMLDivElement;
let root: Root;

beforeEach(() => {
  // Stand-in for the onboard view: transformed, so it is a stacking context.
  view = document.createElement('div');
  view.style.transform = 'translateY(0)';
  document.body.appendChild(view);
  root = createRoot(view);
});

afterEach(async () => {
  await act(async () => {
    closeModal();
    root.unmount();
  });
  view.remove();
});

describe('onboard overlays', () => {
  it('render the removal dialog on body, outside the transformed view', async () => {
    await act(async () => {
      root.render(<ObModals />);
      openModal({ kind: 'disconnect', systemId: 'cmms-ind', systemName: 'Plant CMMS' });
    });
    const overlay = document.querySelector('.ob-modal-overlay');
    expect(overlay).not.toBeNull();
    expect(view.contains(overlay)).toBe(false);
    expect(overlay!.parentElement).toBe(document.body);
  });
});
