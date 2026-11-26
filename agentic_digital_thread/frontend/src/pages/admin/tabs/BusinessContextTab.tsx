// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Admin → Context tab — the standing steward surface for per-system business
 * context, OUTSIDE the onboarding flow. The interview's comfort note ("you can
 * edit this later") points HERE, so this tab must always exist for onboarded
 * systems. Reuses <BusinessContextCard/> verbatim: same GET/PUT contract, same
 * untrusted-text handling (controlled textarea value only — never a sink),
 * same honest runtime-roll warning. This file adds only system selection. */
import { useEffect, useState } from 'react';

import { requestJson } from '../../../api/client';
import { BusinessContextCard } from '../../../views/onboard/components/BusinessContextCard';

type CatalogEntry = {
  id?: string;
  system_id?: string;
  name?: string;
  status?: string;
};

const MUTED = 'var(--muted)';

function entryId(e: CatalogEntry): string {
  return String(e.id || e.system_id || '');
}

export default function BusinessContextTab() {
  const [systems, setSystems] = useState<CatalogEntry[]>([]);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const raw = await requestJson<unknown>('/api/onboard/catalog');
        const entries: CatalogEntry[] = Array.isArray(raw)
          ? (raw as CatalogEntry[])
          : ((raw as { systems?: CatalogEntry[]; catalog?: CatalogEntry[] })?.systems ??
            (raw as { catalog?: CatalogEntry[] })?.catalog ??
            []);
        const onboarded = entries.filter(
          (e) => e.status === 'onboarded' && entryId(e),
        );
        if (!live) return;
        setSystems(onboarded);
        if (onboarded.length > 0) setSelected(entryId(onboarded[0]));
      } catch {
        if (live) setError('Could not load the system list.');
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  return (
    <div>
      <p style={{ fontSize: '0.8rem', color: MUTED, margin: '0 0 0.8rem' }}>
        Business context is the plain-language knowledge your agents read about
        each system — what the data means, what to exclude, what is not
        covered. It was drafted during onboarding; edit it here any time.
      </p>
      {loading && <p style={{ color: MUTED }}>Loading systems…</p>}
      {!loading && error && <p role="alert">{error}</p>}
      {!loading && !error && systems.length === 0 && (
        <p style={{ color: MUTED }}>
          No onboarded systems yet — context becomes editable here after a
          system is onboarded.
        </p>
      )}
      {systems.length > 0 && (
        <div>
          <div
            role="tablist"
            aria-label="System"
            style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginBottom: '0.8rem' }}
          >
            {systems.map((s) => {
              const id = entryId(s);
              const active = id === selected;
              return (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => setSelected(id)}
                  style={{
                    fontSize: '0.78rem',
                    padding: '0.25rem 0.7rem',
                    borderRadius: 999,
                    // Active affordance mirrors admin.css's .filter-bar
                    // button.active (accent border + accent text) on the real
                    // --surface2 theme var (both themes define it).
                    border: active ? '1px solid var(--accent)' : '1px solid var(--border)',
                    background: active ? 'var(--surface2)' : 'transparent',
                    color: active ? 'var(--accent)' : 'var(--text)',
                    fontWeight: active ? 600 : 400,
                    cursor: 'pointer',
                  }}
                >
                  {s.name || id}
                </button>
              );
            })}
          </div>
          {selected && <BusinessContextCard key={selected} systemId={selected} />}
        </div>
      )}
    </div>
  );
}
