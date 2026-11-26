// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* KgNodeSearch — find a node in the Knowledge Graph view by name. Matches
 * against the nodes of the CURRENT view (kg-view-store, no refetch); picking
 * a result selects and fits it through focusKgNode. Plain English strings,
 * like the rest of the Graph Explorer panel. */
import { useState } from 'react';

import type { NeptuneNode } from '../../api/types/neptune';
import { getCurrentViewData } from './kg-view-store';
import { focusKgNode } from './neptune-graph';

/** Case-insensitive substring match on a node's label (or id), name-prefix
 * matches first, then alphabetical; at most `limit` rows. */
export function matchKgNodes(nodes: readonly NeptuneNode[], query: string, limit = 8): NeptuneNode[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const name = (n: NeptuneNode) => String(n.label || n.id || '').toLowerCase();
  return nodes
    .filter((n) => name(n).includes(q))
    .sort((a, b) => {
      const pa = name(a).startsWith(q) ? 0 : 1;
      const pb = name(b).startsWith(q) ? 0 : 1;
      return pa - pb || name(a).localeCompare(name(b));
    })
    .slice(0, limit);
}

export function KgNodeSearch() {
  const [query, setQuery] = useState('');
  const [note, setNote] = useState('');
  const hits = matchKgNodes(getCurrentViewData()?.nodes ?? [], query);
  const pick = (n: NeptuneNode) => {
    setNote(focusKgNode(n.id) ? '' : 'Not shown in the current view. Turn its node type on.');
  };
  return (
    <div className="kg-node-search">
      <input
        type="search"
        className="kg-node-search-input"
        placeholder="Find a node by name"
        aria-label="Find a node by name"
        value={query}
        onChange={(e) => { setQuery(e.target.value); setNote(''); }}
      />
      {hits.length > 0 && (
        <ul className="kg-node-search-hits">
          {hits.map((n) => (
            <li key={n.id}>
              <button type="button" onClick={() => pick(n)}>
                {n.label || n.id} <span className="kg-node-search-type">{n.type}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {query.trim() && hits.length === 0 && <div className="kg-node-search-note">No node matches.</div>}
      {note && <div className="kg-node-search-note">{note}</div>}
    </div>
  );
}
