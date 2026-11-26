// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* lib/system-names.ts — the ONE system display-name resolver. Unit rules:
 * catalog name first (exact id, then case / `_`-vs-`-` insensitive); a value
 * that already is a name comes back verbatim; an unknown id gets the readable
 * fallback; names are added or renamed, never dropped; subscribers re-render
 * when names arrive. */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';

import {
  _resetSystemNames,
  catalogSystemName,
  fallbackSystemName,
  rememberSystemNames,
  systemName,
  useSystemNamesVersion,
} from '../lib/system-names';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => _resetSystemNames());

describe('systemName', () => {
  it('returns the catalog name for a known id', () => {
    rememberSystemNames([{ id: 'mes-ind', name: 'Riverside MES' }]);
    expect(systemName('mes-ind')).toBe('Riverside MES');
  });

  it('matches the local query_* underscore form and any case', () => {
    rememberSystemNames([{ id: 'cmms-ind', name: 'Riverside Plant Maintenance' }]);
    expect(systemName('cmms_ind')).toBe('Riverside Plant Maintenance');
    expect(systemName('CMMS-IND')).toBe('Riverside Plant Maintenance');
  });

  it('prefers an exact id over a sibling that normalises the same', () => {
    rememberSystemNames([
      { id: 'mes_x', name: 'Underscore' },
      { id: 'mes-x', name: 'Dash' },
    ]);
    expect(systemName('mes_x')).toBe('Underscore');
    expect(systemName('mes-x')).toBe('Dash');
  });

  it('reads onboard-catalog entries keyed by system_id too', () => {
    rememberSystemNames([{ system_id: 'plm', name: 'Product Lifecycle' }]);
    expect(systemName('plm')).toBe('Product Lifecycle');
  });

  it('falls back to a readable name for an id the catalog does not know', () => {
    expect(systemName('mes-ind')).toBe('MES Ind');
    expect(systemName('quality_lab')).toBe('Quality Lab');
    expect(systemName('sitewise-direct')).toBe('SiteWise');
    expect(fallbackSystemName('erp')).toBe('ERP');
  });

  it('returns a value that is already a name verbatim', () => {
    rememberSystemNames([{ id: 'mes-ind', name: 'Riverside MES' }]);
    expect(systemName('Riverside MES')).toBe('Riverside MES');
    expect(systemName('Some Other System')).toBe('Some Other System');
  });

  it('is empty for an empty id and ignores entries without a name', () => {
    rememberSystemNames([{ id: 'x-1', name: '  ' }, { id: '', name: 'Nameless' }, null as never]);
    expect(systemName('')).toBe('');
    expect(systemName(null)).toBe('');
    expect(catalogSystemName('x-1')).toBeNull();
  });

  it('renames in place and never drops a name a later catalog omits', () => {
    rememberSystemNames([{ id: 'a', name: 'First' }, { id: 'b', name: 'Bee' }]);
    rememberSystemNames([{ id: 'a', name: 'Renamed' }]);
    expect(systemName('a')).toBe('Renamed');
    expect(systemName('b')).toBe('Bee');
  });
});

describe('useSystemNamesVersion', () => {
  it('re-renders a subscriber when catalog names arrive', () => {
    function Label() {
      useSystemNamesVersion();
      return <span>{systemName('mes-ind')}</span>;
    }
    const host = document.createElement('div');
    const root = createRoot(host);
    act(() => root.render(<Label />));
    expect(host.textContent).toBe('MES Ind');
    act(() => rememberSystemNames([{ id: 'mes-ind', name: 'Riverside MES' }]));
    expect(host.textContent).toBe('Riverside MES');
    act(() => root.unmount());
  });

  it('does not notify when nothing changed', () => {
    let renders = 0;
    function Probe() {
      useSystemNamesVersion();
      renders += 1;
      return null;
    }
    rememberSystemNames([{ id: 'mes-ind', name: 'Riverside MES' }]);
    const root = createRoot(document.createElement('div'));
    act(() => root.render(<Probe />));
    const before = renders;
    act(() => rememberSystemNames([{ id: 'mes-ind', name: 'Riverside MES' }]));
    expect(renders).toBe(before);
    act(() => root.unmount());
  });
});
