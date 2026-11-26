// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — G3 session-systems accumulation (chat/session-systems.ts), the port
 * of the merged legacy window._sessionSystemsUsed delta: canonical system ids
 * from gateway tool-name prefixes feed the save-prompt required-systems
 * prefill; non-prefixed tools are ignored; never cleared between turns. */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  _resetSessionSystems,
  noteSessionSystem,
  sessionSystemsPrefill,
} from '../chat/session-systems';

beforeEach(() => {
  _resetSessionSystems();
});

describe('session-systems (G3)', () => {
  it('accumulates canonical ids from "<system_id>___<tool>" prefixes, deduped + sorted', () => {
    noteSessionSystem('mes___query_mes');
    noteSessionSystem('erp___query_erp');
    noteSessionSystem('erp___query_erp'); // dedupe
    expect(sessionSystemsPrefill()).toBe('erp, mes');
  });

  it('ignores non-prefixed tools (explore_graph etc.) and non-strings', () => {
    noteSessionSystem('explore_graph');
    noteSessionSystem(undefined);
    noteSessionSystem(null);
    noteSessionSystem(42);
    expect(sessionSystemsPrefill()).toBe('');
  });

  it('persists across turns (never cleared on new turns)', () => {
    noteSessionSystem('cmms___query_cmms');
    // a "new turn" does nothing special — the set persists
    noteSessionSystem('plm___query_plm');
    expect(sessionSystemsPrefill()).toBe('cmms, plm');
  });
});
