// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Shared onboarding utilities. */

import { escapeHtml as obEsc } from '../../lib/esc';

// HTML-escape any source-derived value (sampled source-system data, discovered
// field/table/concept names, cross-system values, agent-authored messages —
// all UNTRUSTED) before interpolating it into a string PAYLOAD. In-page
// onboarding content renders as JSX (escaped by construction); this helper
// remains for string-building call sites and their tests.
// Re-exported legacy name for the shared helper in src/lib/esc.ts.
export { obEsc };

// TYPED-connector read-only connection summary. Returns a NON-SECRET summary
// string for Athena ('workgroup · database · region') and SiteWise (Direct)
// ('region · role name'), whose real settings live in structured
// connection_config rather than a connection string — or null for every other
// type (PostgreSQL / MCP Server / REST API / SiteWise (i3X)), which keeps the
// editable connection_string input. NEVER includes external_id or any secret;
// for SiteWise only the role NAME (last ARN path segment) is shown, never the
// full ARN/account. Caller escapes the returned string with obEsc().
export function obTypedConnSummary(s: {
  connection_type?: string;
  connection_config?: unknown;
}): string | null {
  const ct = String((s && s.connection_type) || '').toLowerCase();
  let cfg: Record<string, unknown> = (s && (s.connection_config as Record<string, unknown>)) || {};
  if (typeof cfg === 'string') {
    try {
      cfg = JSON.parse(cfg);
    } catch {
      cfg = {};
    }
  }
  if (typeof cfg !== 'object' || cfg === null) cfg = {};
  if (ct.includes('athena')) {
    const parts = [cfg.workgroup, cfg.database, cfg.region].filter(Boolean).map(String);
    return parts.length ? parts.join(' · ') : '—';
  }
  if (ct.includes('sitewise') && ct.includes('direct')) {
    const roleName = String(cfg.role_arn || '').split('/').pop();
    const parts = [cfg.region, roleName].filter(Boolean).map(String);
    return parts.length ? parts.join(' · ') : '—';
  }
  return null;
}

/** Friendly labels for onboarding-agent tool pills (onboard.js _toolLabel).
 * A tool served through the onboarding gateway arrives prefixed with its
 * target (`onboarding___discover`); the label is the bare tool's, and a tool
 * without a label shows its bare name (never the prefix).
 * reset_system's label stays true for every outcome: it may start over a
 * half-done onboarding, remove the system, be refused, or hand removal off
 * to the system's card, and the agent's text says which. */
export function toolLabel(name: string): string {
  const labels: Record<string, string> = {
    get_catalog: 'Checking catalog',
    get_system_info: 'Reading system info',
    test_connection: 'Testing connection',
    discover: 'Discovering schema',
    map_fields: 'Mapping fields',
    resolve_identities: 'Resolving identities',
    provision_system: 'Provisioning system',
    verify_system: 'Verifying',
    reset_system: 'Checking removal',
  };
  const bare = String(name || '').split('___').pop() || '';
  return labels[bare] || bare || name;
}

