// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, expect, it } from 'vitest';

import { neptunePillLabel } from '../chat/tool-labels';

describe('neptunePillLabel reads node labels in the query, not words in the result', () => {
  it('labels a node-type count as a knowledge graph query', () => {
    const src = 'match (n) return labels(n) as node_type, count(*) {"rows": [["authorityrule", 116], ["concept", 80]]}';
    expect(neptunePillLabel(src)).toBe('KG: Cypher query');
  });

  it('labels a generic Cypher query as a KG query, not an authority-rule lookup', () => {
    expect(neptunePillLabel('MATCH (a)-[r]->(b) RETURN type(r), count(*)')).toBe('KG: Cypher query');
  });

  it('labels a query that matches authority rules', () => {
    expect(neptunePillLabel('MATCH (r:AuthorityRule) RETURN r LIMIT 5')).toBe('Querying authority rules');
  });

  it('labels a concept query', () => {
    expect(neptunePillLabel('MATCH (f:Field)-[:HAS_CONCEPT]->(c:Concept) RETURN c.name')).toBe('Querying concepts');
  });
});
