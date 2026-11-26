// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <SystemConnections/> — the "Connections" section of the system detail
 * page: every join path (either direction), value mapping and identity rule
 * that touches the system, each with what proved it. One bounded GET
 * (/api/onboard/connections/{id}); a long list shows "first N of M". */
import { useEffect, useState } from 'react';

import { requestJson } from '../../../api/client';
import type {
  ConnectionIdentityRule,
  SystemConnectionsResponse,
} from '../../../api/types/onboard';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import { displayTable } from '../connection-evidence';
import { JoinEvidence } from './ResolveExtras';

type LoadState =
  | { phase: 'loading' }
  | { phase: 'error'; text: string }
  | { phase: 'ready'; data: SystemConnectionsResponse };

const muted = { color: 'var(--muted)', fontSize: '0.75rem' } as const;
const tableStyle = { width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' } as const;
const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '0.4rem 0.5rem',
  color: 'var(--muted)',
  fontWeight: 500,
};

/** "12 join paths" or "first 50 of 450 value mappings". */
export function shownOf(shown: number, total: number | undefined, one: string, many: string): string {
  const all = typeof total === 'number' ? Math.max(total, shown) : shown;
  const noun = all === 1 ? one : many;
  return shown < all
    ? `first ${shown.toLocaleString()} of ${all.toLocaleString()} ${noun}`
    : `${all.toLocaleString()} ${noun}`;
}

function endpoint(system?: string, table?: string, field?: string) {
  const path = [displayTable(table), field].filter(Boolean).join('.');
  return (
    <>
      {systemName(system)} {path ? <code>{path}</code> : null}
    </>
  );
}

/** What proved an identity rule, in plain words. */
export function identityRuleEvidence(r: ConnectionIdentityRule): string {
  const parts: string[] = [];
  if (r.approved_by) parts.push(`Approved by ${r.approved_by}.`);
  const note = String(r.origin_context || r.provenance || '').trim();
  if (note) parts.push(note.endsWith('.') ? note : `${note}.`);
  if (!parts.length) parts.push('Confirmed at the Resolve step.');
  return parts.join(' ');
}

export function SystemConnections({ systemId }: { systemId: string }) {
  useSystemNamesVersion();
  const [state, setState] = useState<LoadState>({ phase: 'loading' });

  useEffect(() => {
    let live = true;
    setState({ phase: 'loading' });
    requestJson<SystemConnectionsResponse>(
      `/api/onboard/connections/${encodeURIComponent(systemId)}`,
    )
      .then((data) => {
        if (!live) return;
        if (data.error) setState({ phase: 'error', text: data.error });
        else setState({ phase: 'ready', data });
      })
      .catch((e: Error) => {
        if (live) setState({ phase: 'error', text: 'Could not load connections: ' + e.message });
      });
    return () => {
      live = false;
    };
  }, [systemId]);

  return (
    <section aria-labelledby="ob-system-connections-title" style={{ marginBottom: '1.25rem' }}>
      <h4
        id="ob-system-connections-title"
        style={{
          fontSize: '0.75rem',
          color: 'var(--muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          margin: '0 0 0.5rem',
        }}
      >
        Connections
      </h4>
      {state.phase === 'loading' && <div style={muted}>Loading connections…</div>}
      {state.phase === 'error' && (
        <div role="alert" style={{ color: 'var(--red)', fontSize: '0.8rem' }}>
          {state.text}
        </div>
      )}
      {state.phase === 'ready' && <ConnectionsBody data={state.data} />}
    </section>
  );
}

function ConnectionsBody({ data }: { data: SystemConnectionsResponse }) {
  const joinPaths = data.join_paths || [];
  const valueMappings = data.value_mappings || [];
  const rules = data.identity_rules || [];
  if (!joinPaths.length && !valueMappings.length && !rules.length) {
    return <div style={muted}>No connections to other tables or systems yet.</div>;
  }
  return (
    <>
      <div style={{ ...muted, marginBottom: '0.5rem' }} data-testid="connections-counts">
        {shownOf(joinPaths.length, data.join_paths_total, 'join path', 'join paths')},{' '}
        {shownOf(rules.length, data.identity_rules_total, 'identity rule', 'identity rules')},{' '}
        {shownOf(valueMappings.length, data.value_mappings_total, 'value mapping', 'value mappings')}
      </div>
      {joinPaths.length > 0 && (
        <table style={tableStyle} aria-label="Join paths">
          <thead>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              <th style={th}>From</th>
              <th style={th}>To</th>
              <th style={th}>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {joinPaths.map((jp, i) => (
              <tr key={jp.id || i}>
                <td>{endpoint(jp.from_system, jp.from_table, jp.from_field)}</td>
                <td>{endpoint(jp.to_system, jp.to_table, jp.to_field)}</td>
                <td>
                  <JoinEvidence jp={jp} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {rules.length > 0 && (
        <table style={{ ...tableStyle, marginTop: '0.75rem' }} aria-label="Identity rules">
          <thead>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              <th style={th}>Concept</th>
              <th style={th}>Fields</th>
              <th style={th}>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r, i) => (
              <tr key={r.id || i}>
                <td>
                  {r.concept || ''}
                  {r.status && r.status !== 'active' ? (
                    <span style={muted}> ({r.status})</span>
                  ) : null}
                </td>
                <td>
                  {endpoint(r.source_system, '', r.source_field)} ↔{' '}
                  {endpoint(r.target_system, '', r.target_field)}
                </td>
                <td style={{ fontSize: '0.75rem' }}>
                  <details>
                    <summary style={{ cursor: 'pointer' }}>
                      {r.source_pattern || r.target_pattern ? (
                        <code>
                          {r.source_pattern || '*'} → {r.target_pattern || '*'}
                        </code>
                      ) : (
                        'same value'
                      )}
                    </summary>
                    <p style={{ margin: '0.35rem 0 0' }}>{identityRuleEvidence(r)}</p>
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {valueMappings.length > 0 && (
        <table style={{ ...tableStyle, marginTop: '0.75rem' }} aria-label="Value mappings">
          <thead>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              <th style={th}>From</th>
              <th style={th}>To</th>
              <th style={th}>Concept</th>
              <th style={th}>How</th>
            </tr>
          </thead>
          <tbody>
            {valueMappings.map((vm, i) => (
              <tr key={vm.id || i}>
                <td>
                  {systemName(vm.from_system)} <code>{vm.from_value || ''}</code>
                </td>
                <td>
                  {systemName(vm.to_system)} <code>{vm.to_value || ''}</code>
                </td>
                <td style={muted}>{vm.concept || ''}</td>
                <td style={muted}>{(vm.method || '').replace(/_/g, ' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
