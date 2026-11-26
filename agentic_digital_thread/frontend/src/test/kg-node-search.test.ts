// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, expect, it } from 'vitest';

import type { NeptuneNode } from '../api/types/neptune';
import { matchKgNodes } from '../views/architecture/KgNodeSearch';

const NODES: NeptuneNode[] = [
  { id: 'n1', label: 'Press Line 3', type: 'Entity' },
  { id: 'n2', label: 'Line 3 Robot', type: 'Entity' },
  { id: 'n3', label: 'work_order', type: 'Concept' },
  { id: 'n4', label: '', type: 'System' },
  { id: 'line-3-cell', label: 'Cell', type: 'Entity' },
];

describe('matchKgNodes', () => {
  it('matches case-insensitively on label, prefix matches first', () => {
    expect(matchKgNodes(NODES, 'line 3').map((n) => n.id)).toEqual(['n2', 'n1']);
  });

  it('falls back to the id when a node has no label', () => {
    expect(matchKgNodes(NODES, 'N4').map((n) => n.id)).toEqual(['n4']);
  });

  it('returns nothing for a blank query and honours the limit', () => {
    expect(matchKgNodes(NODES, '   ')).toEqual([]);
    expect(matchKgNodes(NODES, 'e', 2)).toHaveLength(2);
  });
});
