// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboarding tool pills never show the gateway's target prefix.
 *
 * Tools served through the onboarding gateway arrive as
 * `onboarding___<tool>`. A labelled tool gets its label; a tool without a
 * label showed the full prefixed name. It now shows the bare tool name. */
import { describe, expect, it } from 'vitest';

import { toolLabel } from '../views/onboard/util';

describe('toolLabel', () => {
  it.each([
    'assess_vocabulary_fit',
    'propose_concepts',
    'connect_new_system',
    'store_credentials',
  ])('shows an unlabelled gateway tool %s by its bare name', (tool) => {
    expect(toolLabel(`onboarding___${tool}`)).toBe(tool);
    expect(toolLabel(tool)).toBe(tool);
  });

  it('labels reset_system, prefixed or bare', () => {
    expect(toolLabel('onboarding___reset_system')).toBe('Checking removal');
    expect(toolLabel('reset_system')).toBe('Checking removal');
  });

  it('labels the other onboarding tools, prefixed or bare', () => {
    expect(toolLabel('onboarding___discover')).toBe('Discovering schema');
    expect(toolLabel('provision_system')).toBe('Provisioning system');
  });

  it('keeps a usable label for an empty or prefix-only name', () => {
    expect(toolLabel('')).toBe('');
    expect(toolLabel('onboarding___')).toBe('onboarding___');
  });
});
