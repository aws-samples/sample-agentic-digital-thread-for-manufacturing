// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* System catalog logic (jsx-migration W5.1) — fetch + actions behind
 * <CatalogList/> (#catalog-list / #card-<id> / #btn-<id> are e2e-load-bearing,
 * rendered by the component), the admin connection Test, the system detail
 * screen, and the Connect New System submit. All markup moved
 * to components/; this module dispatches store actions and keeps the API
 * calls + payload builders. */
import { ApiError, requestJson } from '../../api/client';
import { queryKeys } from '../../api/query-keys';
import type {
  CatalogSystem,
  ConnField,
  DiscoveredTable,
  FieldMapping,
} from '../../api/types/onboard';
import {
  beginConnectNewAttempt,
  catalogStore,
  failConnectNewAttempt,
  finishConnectNewAttempt,
  setConnectNewError,
  setConnectNewOpen,
  setTestResult,
  toggleConnOpen,
} from './catalog-store';
import {
  fetchCatalog,
  getOnboardQueryClient,
  invalidateOnboardingConnect,
} from './onboard-queries';
import { obGuardStart, obReset } from './flow';
import { openDetail, restoreSaved, setDetail } from './flow-store';
import { refreshSuggestions } from './chat';
import {
  buildAthenaConnectionConfig,
  buildConnectBody,
  buildI3xConnectBody,
  buildTulipConnectBody, // Tulip is a first-class connection type
  buildTulipArnConnectBody, // Tulip with a named Secrets Manager secret source
  buildEditConnectionBody,
  buildPostgresConnectBody,
  buildSiteWiseDirectConnectionConfig,
  buildUrlConnectBody,
  extractPostgresSchema,
} from './payloads';
import { obCheckAdmin } from './state';

export async function obCheckSetup(): Promise<boolean> {
  // Industry pack wizard removed — core concepts are seeded by deploy.sh
  return false;
}

export async function obLoadCatalog(): Promise<void> {
  if (await obCheckSetup()) return; // show picker first
  void refreshSuggestions();
  await obCheckAdmin(); // resolve admin gate before rendering Test/Retest buttons
  try {
    await fetchCatalog(getOnboardQueryClient());
  } catch {
    // The mounted query owns and exposes the error state. Keep void legacy
    // callers from producing an unhandled rejection.
  }
}

/** Legacy signature kept for the window shim (`toggleConn('conn-<sid>', …)`);
 * the React toggle calls the store action directly. */
export function toggleConn(connId: string, _toggleId?: string): void {
  toggleConnOpen(connId.replace(/^conn-/, ''));
}

// Admin-only: test/retest a system's connection via the admin endpoint and
// render a compact pass/fail + latency result. The response carries no secrets
// (status / latency_ms / connection_type / detail only); never render a token.
export async function obTestConnection(systemId: string): Promise<void> {
  setTestResult(systemId, { state: 'testing' });
  const fail = (text: string) =>
    setTestResult(systemId, {
      state: 'done',
      color: 'var(--red, #f87171)',
      text,
      ran: true,
    });
  try {
    const d = await requestJson<{
      status?: string;
      latency_ms?: number;
      detail?: string;
    }>('/api/admin/connection/' + encodeURIComponent(systemId) + '/test', {
      method: 'POST',
    });
    if (d.status === 'pass') {
      const lat = d.latency_ms != null ? d.latency_ms + ' ms' : '';
      setTestResult(systemId, {
        state: 'done',
        color: 'var(--green, #4ade80)',
        text: '✓ Connected' + (lat ? ' · ' + lat : ''),
        ran: true,
      });
    } else {
      fail('✗ Failed · ' + (d.detail || 'connection error'));
    }
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) fail('✗ Admin only');
    else if (e instanceof ApiError && e.status === 404) fail('✗ Unknown system');
    else fail('✗ Error: ' + (e as Error).message);
  }
}

// ── Admin-only: Edit connection / Update ─────────────────────────────────────

/** PUT the edited connection (the <EditConnectionForm/> submit). Omits the
 * secret field when blank (keep current). Returns the {color, text} result
 * line; never returns a secret (server strings render as plain React text). */
export async function submitEditConnection(
  systemId: string,
  fields: ConnField[],
  values: Record<string, string>,
): Promise<{ color: string; text: string } | null> {
  const body = buildEditConnectionBody(fields, values);
  const RED = 'var(--red, #f87171)';
  try {
    const d = await requestJson<{
      status?: string;
      reason?: string;
      detail?: string;
      warnings?: string[];
    }>(
      '/api/admin/connection/' + encodeURIComponent(systemId),
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    );
    if (d.status === 'ok') {
      // Saved, but something was not checked (e.g. a PostgreSQL login's
      // privileges could not be read): say so in amber.
      const warning = (d.warnings || []).find((w) => typeof w === 'string' && w.trim());
      if (warning) return { color: 'var(--amber, #fbbf24)', text: '✓ Reconnected · ' + warning };
      return { color: 'var(--green, #4ade80)', text: '✓ Reconnected' };
    }
    if (d.status === 'validation_failed') {
      return {
        color: RED,
        text: '✗ Validation failed · ' + (d.reason || d.detail || 'probe failed'),
      };
    }
    return { color: RED, text: '✗ Update failed' };
  } catch (e) {
    if (e instanceof ApiError && e.status === 403)
      return { color: RED, text: '✗ Admin only' };
    if (e instanceof ApiError && e.status === 404)
      return { color: RED, text: '✗ Unknown system' };
    if (e instanceof ApiError && e.status === 400)
      return { color: RED, text: '✗ Validation failed · ' + e.detail };
    return { color: RED, text: '✗ Error: ' + (e as Error).message };
  }
}

// ── System detail ────────────────────────────────────────────────────────────
// Re-map (remapSystem / saveRemappings + the remap review screen) was removed
// for the 2026-10 release: its Save POSTed /api/onboard/provision, the full
// re-provision, which deletes a system's onboarding identity rules and join
// paths, replaces its whole Field subgraph with the rows sent, and reseeds the
// agent from the mapper's roles. See the note in components/SystemDetail.tsx
// for what a field-only remap path needs.

export async function showSystemDetail(sys: CatalogSystem): Promise<void> {
  openDetail(sys); // snapshots the current flow (the _obSavedFlow save)
  try {
    const [discResp, mapResp] = await Promise.all([
      requestJson<{ tables?: DiscoveredTable[] }>('/api/onboard/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ system_id: sys.id }),
      }),
      requestJson<{ mappings?: FieldMapping[] }>(`/api/onboard/mappings/${sys.id}`),
    ]);
    const data = discResp;
    const mapData = mapResp;
    setDetail({
      phase: 'ready',
      sys,
      tables: data.tables || [],
      mappings: mapData.mappings || [],
    });
  } catch (e) {
    setDetail({
      phase: 'error',
      sys,
      tables: [],
      mappings: [],
      errorText: 'Failed to load details: ' + (e as Error).message,
    });
  }
}

export function obRestoreFlow(): void {
  if (!restoreSaved()) void obReset();
}

// ── Connect New System ───────────────────────────────────────────────────────

/** Shown when REST API (OpenAPI) is chosen: the server refuses the type too. */
export const REST_UNSUPPORTED =
  'REST API (OpenAPI) systems are not supported yet: this build cannot provision a ' +
  'generic REST connector. Tulip and AWS IoT SiteWise (i3X) have their own connection types.';

export function showConnectNewForm(): void {
  setConnectNewOpen(true);
}

/** The <ConnectNewModal/> field values — React state, not DOM reads. */
export interface ConnectNewValues {
  connType: string;
  name: string;
  /** Optional site the system serves (e.g. a plant name, or All Sites). Blank
   * registers the system with no plant. */
  plant?: string;
  /** Connection string / spec URL / MCP URL (non-typed connectors). */
  endpoint: string;
  /** MCP Server — optional token (→ connection_config.token → Secrets
   * Manager adt/conn/<id>; never stored on the catalog or the wizard state). */
  mcpToken?: string;
  athena: {
    workgroup: string;
    database: string;
    catalog: string;
    region: string;
    output: string;
    roleArn: string;
    externalId: string;
  };
  sitewise: { region: string; roleArn: string; externalId: string };
  /** SiteWise (i3X) — endpoint URL + bearer token (token → Secrets Manager). */
  i3x: { endpoint: string; token: string };
  /** Tulip (Tables API) — base URL + raw api_key/api_secret
   * (the server encodes them to an HTTP Basic token → Secrets Manager).
   * OPTIONAL workspace_id scopes calls to /api/v3/w/<id>/ for an instance-wide
   * Service Account; blank ⇒ the unscoped path a workspace-scoped key uses.
   * ``credentialMode`` chooses the credential SOURCE: 'inline' (api_key +
   * api_secret, today's fields) or 'secret_arn' (name an existing Secrets
   * Manager secret). The ARN mode is offered only when the deploy enables it
   * for Tulip (secretArnSourceOffered); its base URL and workspace are OPTIONAL
   * because the named secret supplies and pins them. */
  tulip: {
    credentialMode?: 'inline' | 'secret_arn';
    baseUrl: string;
    workspaceId: string;
    apiKey: string;
    apiSecret: string;
    secretArn?: string;
  };
}

/** Connect New System submit. The form stays open, keeping every typed
 * value, until registration succeeds: a validation problem or a failed
 * request is shown inline in the form (setConnectNewError), and only a
 * successful registration closes it and starts onboarding. */
export async function startCustomOnboard(v: ConnectNewValues): Promise<void> {
  if (catalogStore.get().connectNewBusy) return;
  const connType = v.connType;
  const name = v.name || 'Custom System';
  // The site this system serves. Blank is no plant at all, never a
  // placeholder: provision retires competing authority claims of every other
  // system at the SAME plant, so a shared placeholder made unrelated systems
  // compete. A typed plant rides in the register body (the catalog entry and
  // card onboarding read it) and into the wizard; a blank one is omitted, so
  // the body is byte-identical to before the field existed.
  const plant = (v.plant || '').trim();
  // The old form's placeholder now reads as no plant on the server, so it
  // can't name a site (agents/catalog.py reserved_plant; same message).
  if (plant.toLowerCase() === 'custom') {
    setConnectNewError("Plant can't be 'Custom'. Leave it blank if the system isn't tied to one site.");
    return;
  }
  const registerBody = (b: Record<string, unknown>): string =>
    JSON.stringify(plant ? { ...b, plant } : b);

  // Athena is a typed connector — no single connection string. Register it via
  // POST /api/onboard/connect with a structured connection_config, then run the
  // standard discover -> map -> resolve -> provision pipeline (obStart carries
  // connection_config into the discover/provision calls).
  if (connType === 'athena') {
    const workgroup = v.athena.workgroup.trim();
    const database = v.athena.database.trim();
    if (!workgroup) {
      setConnectNewError('Athena workgroup is required');
      return;
    }
    if (!database) {
      setConnectNewError('Athena database/schema is required');
      return;
    }
    const connection_config = buildAthenaConnectionConfig({
      workgroup,
      database,
      catalog: v.athena.catalog.trim(),
      region: v.athena.region.trim(),
      outputLocation: v.athena.output.trim(),
      roleArn: v.athena.roleArn.trim(),
      externalId: v.athena.externalId.trim(),
    });
    const attempt = beginConnectNewAttempt();
    try {
      const data = await requestJson<{ error?: string; system_id?: string }>(
        '/api/onboard/connect',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: registerBody(buildConnectBody(name, 'Athena', connection_config)),
        },
      );
      if (data.error) {
        failConnectNewAttempt(attempt, 'Failed to register Athena system: ' + data.error);
        return;
      }
      await invalidateOnboardingConnect(getOnboardQueryClient());
      if (!finishConnectNewAttempt(attempt)) return;
      obGuardStart({
        id: data.system_id,
        name: name,
        schema: '',
        connection_string: '',
        connection_type: 'Athena',
        connection_config: connection_config,
        plant,
        system_type: 'Custom',
        status: 'registered',
      });
    } catch (e) {
      failConnectNewAttempt(attempt, 'Failed to register Athena system: ' + (e as Error).message);
    }
    return;
  }

  // AWS IoT SiteWise (Direct) is a typed connector with IAM-role auth — no
  // connection string, no token. Register it via POST /api/onboard/connect with a
  // structured connection_config carrying only the (optional) Region, then run the
  // standard discover -> map -> resolve -> provision pipeline.
  if (connType === 'sitewise-direct') {
    const connection_config = buildSiteWiseDirectConnectionConfig({
      region: v.sitewise.region.trim(),
      roleArn: v.sitewise.roleArn.trim(),
      externalId: v.sitewise.externalId.trim(),
    });
    const attempt = beginConnectNewAttempt();
    try {
      const data = await requestJson<{ error?: string; system_id?: string }>(
        '/api/onboard/connect',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: registerBody(
            buildConnectBody(name, 'SiteWise (Direct)', connection_config),
          ),
        },
      );
      if (data.error) {
        failConnectNewAttempt(attempt, 'Failed to register SiteWise system: ' + data.error);
        return;
      }
      await invalidateOnboardingConnect(getOnboardQueryClient());
      if (!finishConnectNewAttempt(attempt)) return;
      obGuardStart({
        id: data.system_id,
        name: name,
        schema: '',
        connection_string: '',
        connection_type: 'SiteWise (Direct)',
        connection_config: connection_config,
        plant,
        system_type: 'Custom',
        status: 'registered',
      });
    } catch (e) {
      failConnectNewAttempt(attempt, 'Failed to register SiteWise system: ' + (e as Error).message);
    }
    return;
  }

  // AWS IoT SiteWise (i3X) — a URL + bearer-token connector. Register via
  // POST /api/onboard/connect: the endpoint rides as connection_string and
  // the token inside connection_config, which the server routes STRAIGHT to
  // Secrets Manager (adt/conn/<id>) — it never lands on the catalog entry.
  // Then the standard discover -> map -> resolve -> provision pipeline runs;
  // discover/provision resolve the token from the registered secret.
  if (connType === 'sitewise-i3x') {
    const i3xEndpoint = v.i3x.endpoint.trim();
    if (!i3xEndpoint) {
      setConnectNewError('i3X endpoint URL is required');
      return;
    }
    const attempt = beginConnectNewAttempt();
    try {
      const data = await requestJson<{ error?: string; system_id?: string }>(
        '/api/onboard/connect',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: registerBody(buildI3xConnectBody(name, i3xEndpoint, v.i3x.token.trim())),
        },
      );
      if (data.error) {
        failConnectNewAttempt(attempt, 'Failed to register i3X system: ' + data.error);
        return;
      }
      await invalidateOnboardingConnect(getOnboardQueryClient());
      if (!finishConnectNewAttempt(attempt)) return;
      obGuardStart({
        id: data.system_id,
        name: name,
        schema: '',
        connection_string: i3xEndpoint,
        connection_type: 'SiteWise (i3X)',
        // Non-secret subset only — the token stays server-side.
        connection_config: { base_url: i3xEndpoint },
        plant,
        system_type: 'Custom',
        status: 'registered',
      });
    } catch (e) {
      failConnectNewAttempt(attempt, 'Failed to register i3X system: ' + (e as Error).message);
    }
    return;
  }

  // Tulip (Tables API) — a first-class connection type
  // declared by the user (no host-sniffing). Base URL rides as connection_string
  // AND connection_config.base_url; the OPTIONAL raw api_key/api_secret ride in
  // connection_config, where the server encodes them to an HTTP Basic token and
  // routes it STRAIGHT to Secrets Manager (adt/conn/<id>) — the sanitized
  // catalog entry never carries the credentials. Then the standard
  // discover -> map -> resolve -> provision pipeline runs.
  if (connType === 'tulip') {
    const tulipBaseUrl = v.tulip.baseUrl.trim();
    const tulipArnMode = v.tulip.credentialMode === 'secret_arn';
    const tulipSecretArn = (v.tulip.secretArn || '').trim();
    if (tulipArnMode) {
      if (!tulipSecretArn) {
        setConnectNewError('A Secrets Manager secret ARN is required');
        return;
      }
    } else if (!tulipBaseUrl) {
      setConnectNewError('Tulip base URL is required');
      return;
    }
    const attempt = beginConnectNewAttempt();
    try {
      const data = await requestJson<{ error?: string; system_id?: string }>(
        '/api/onboard/connect',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: registerBody(
            tulipArnMode
              ? buildTulipArnConnectBody(
                  name,
                  tulipSecretArn,
                  tulipBaseUrl,
                  v.tulip.workspaceId.trim(),
                )
              : buildTulipConnectBody(
                  name,
                  tulipBaseUrl,
                  v.tulip.apiKey.trim(),
                  v.tulip.apiSecret.trim(),
                  v.tulip.workspaceId.trim(),
                ),
          ),
        },
      );
      if (data.error) {
        failConnectNewAttempt(attempt, 'Failed to register Tulip system: ' + data.error);
        return;
      }
      await invalidateOnboardingConnect(getOnboardQueryClient());
      if (!finishConnectNewAttempt(attempt)) return;
      obGuardStart({
        id: data.system_id,
        name: name,
        schema: '',
        connection_string: tulipBaseUrl,
        connection_type: 'Tulip (Tables API)',
        // Non-secret subset only — the credentials stay server-side (inline in
        // adt/conn/<id>, or in the named secret for the ARN source). The
        // OPTIONAL workspace_id is non-secret and rides along; an ARN system
        // records credential_source so the card and resolver mark it pinned.
        connection_config: {
          ...(tulipBaseUrl ? { base_url: tulipBaseUrl } : {}),
          ...(v.tulip.workspaceId.trim()
            ? { workspace_id: v.tulip.workspaceId.trim() }
            : {}),
          ...(tulipArnMode ? { credential_source: 'secret_arn' } : {}),
        },
        plant,
        // Same class the register body sent (buildTulipConnectBody).
        system_type: 'MES',
        status: 'registered',
      });
    } catch (e) {
      failConnectNewAttempt(attempt, 'Failed to register Tulip system: ' + (e as Error).message);
    }
    return;
  }

  const endpoint = v.endpoint;
  // Generic REST API provisioning is not implemented, and the server refuses
  // the registration; say so here instead of sending a request that must fail.
  if (connType === 'rest-api') {
    setConnectNewError(REST_UNSUPPORTED);
    return;
  }
  if (!endpoint) {
    setConnectNewError('Enter a connection endpoint');
    return;
  }
  // Validate BEFORE closing the modal, so a missing search_path leaves the
  // typed connection string in place to fix (it used to close first and
  // lose the input).
  const pgSchema = connType === 'postgresql' ? extractPostgresSchema(endpoint) : '';
  if (connType === 'postgresql' && !pgSchema) {
    setConnectNewError('Connection string must include search_path parameter');
    return;
  }
  const attempt = beginConnectNewAttempt();
  // Every type registers through POST /api/onboard/connect and onboards the id
  // the SERVER returns (a client-minted id is unknown to the catalog, so the
  // wizard's first step, preflight, answered 404 "Unknown system_id").
  const connectionType = connType === 'postgresql' ? 'PostgreSQL' : 'MCP Server';
  // PostgreSQL: the connection string carries the password. It rides ONLY in
  // connection_config.dsn (the server stores it in Secrets Manager) and the
  // wizard keeps no copy: discover/preflight resolve the registered connection
  // server-side.
  const body =
    connectionType === 'PostgreSQL'
      ? buildPostgresConnectBody(name, pgSchema, endpoint)
      : buildUrlConnectBody(name, connectionType, endpoint, v.mcpToken);
  try {
    const data = await requestJson<{ error?: string; system_id?: string }>(
      '/api/onboard/connect',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: registerBody(body),
      },
    );
    if (data.error) {
      failConnectNewAttempt(
        attempt,
        `Failed to register ${connectionType} system: ` + data.error,
      );
      return;
    }
    await invalidateOnboardingConnect(getOnboardQueryClient());
    if (!finishConnectNewAttempt(attempt)) return;
    obGuardStart({
      id: data.system_id,
      name: name,
      schema: pgSchema,
      connection_string: connectionType === 'PostgreSQL' ? '' : endpoint,
      connection_type: connectionType,
      plant,
      system_type: 'Custom',
      status: 'registered',
    });
  } catch (e) {
    failConnectNewAttempt(
      attempt,
      `Failed to register ${connectionType} system: ` + (e as Error).message,
    );
  }
}

// Best-effort check: has a system reached 'onboarded' server-side? Used by the
// provision poll to treat a completed-but-slow (or job-lost-after-restart)
// provision as success instead of a false timeout. DRY — one catalog probe,
// tolerant of both the array and {systems:[...]} catalog shapes.
export async function obSystemIsOnboarded(
  sysId: string,
  signal?: AbortSignal,
): Promise<boolean> {
  try {
    const systems = await fetchCatalog(getOnboardQueryClient(), true, signal);
    const entry = systems.find((system) => (system.id || system.system_id) === sysId);
    return !!(entry && entry.status === 'onboarded');
  } catch (error) {
    if (
      error !== null &&
      typeof error === 'object' &&
      'name' in error &&
      error.name === 'AbortError'
    ) {
      throw error;
    }
    return false;
  }
}

/** Read-only Query cache access for imperative compatibility consumers. */
export function catalogSystems(): CatalogSystem[] {
  return (
    getOnboardQueryClient().getQueryData<CatalogSystem[]>(queryKeys.catalog()) || []
  );
}
