// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure payload builders — the request bodies onboard.js assembles inline,
 * extracted so field names (the API contract) are unit-testable. Every key
 * below is the serialized API contract; do not rename.
 * No DOM, no fetch. */
import type {
  CatalogSystem,
  ConnField,
  FieldMapping,
  IdentityRule,
  RemapSuggestion,
  ScanBudgetPayload,
} from '../../api/types/onboard';
import type { MappingRowVm } from './flow-store';

// ── Mapping-checkpoint confirmation (React state → payload) ─────────────────

/** Derive the confirmed mappings the resolve/provision steps consume from the
 * mapping checkpoint's REACT STATE rows (jsx-migration W5.1 — the old code
 * scraped `.concept-tag` DOM nodes). Skip tags are dropped; a row with no
 * remaining tags falls back to needs_review, exactly like the DOM path.
 * `rows` null (no state available) keeps the mapping's own concepts. */
export function confirmedMappingsFromRows(
  mappings: FieldMapping[],
  rows: readonly MappingRowVm[] | null,
): FieldMapping[] {
  return mappings.map((m, i) => {
    const row = rows?.[i];
    const tags = row
      ? row.tags
          .filter((t) => !t.skip)
          .map((t) => ({ concept: t.concept, confidence: t.confidence }))
      : m.concepts || [{ concept: m.concept, confidence: m.confidence }];
    const primary = tags[0] || { concept: 'needs_review', confidence: 0 };
    return {
      ...m,
      concept: primary.concept,
      confidence: primary.confidence,
      concepts: tags,
    };
  });
}

// ── Connect (POST /api/onboard/connect) ──────────────────────────────────────

export interface AthenaFormValues {
  workgroup: string;
  database: string;
  catalog?: string;
  region?: string;
  outputLocation?: string;
  roleArn?: string;
  externalId?: string;
}

/** Athena is a typed connector: non-secret settings ride in a structured
 * connection_config (workgroup/database/catalog/region/output_location).
 * OPTIONAL cross-account keys (role_arn / external_id) are included ONLY when
 * non-empty so a blank form keeps the app's own (ambient) account credentials.
 * (onboard.js startCustomOnboard, athena branch.) */
export function buildAthenaConnectionConfig(
  v: AthenaFormValues,
): Record<string, string> {
  const connection_config: Record<string, string> = {
    workgroup: v.workgroup,
    database: v.database,
    catalog: v.catalog || 'AwsDataCatalog',
  };
  if (v.region) connection_config.region = v.region;
  if (v.outputLocation) connection_config.output_location = v.outputLocation;
  if (v.roleArn) connection_config.role_arn = v.roleArn;
  if (v.externalId) connection_config.external_id = v.externalId;
  return connection_config;
}

export interface SiteWiseDirectFormValues {
  region?: string;
  roleArn?: string;
  externalId?: string;
}

/** AWS IoT SiteWise (Direct) — IAM-role auth, no URL, no token. Region rides in
 * connection_config.region (blank ⇒ the app's ambient AWS_REGION); the OPTIONAL
 * cross-account Role ARN + External ID flow into connection_config.role_arn /
 * connection_config.external_id so a SiteWise source in ANOTHER account can be
 * discovered and queried via an assumed role. Keys included ONLY when non-empty.
 * (onboard.js startCustomOnboard, sitewise-direct branch.) */
export function buildSiteWiseDirectConnectionConfig(
  v: SiteWiseDirectFormValues,
): Record<string, string> {
  const connection_config: Record<string, string> = {};
  if (v.region) connection_config.region = v.region;
  if (v.roleArn) connection_config.role_arn = v.roleArn;
  if (v.externalId) connection_config.external_id = v.externalId;
  return connection_config;
}

/** POST /api/onboard/connect body for a typed connector.
 * Athena: {name, system_type:'Custom', connection_type:'Athena', connection_config}
 * SiteWise: connection_type 'SiteWise (Direct)'. */
export function buildConnectBody(
  name: string,
  connectionType: 'Athena' | 'SiteWise (Direct)',
  connectionConfig: Record<string, string>,
): Record<string, unknown> {
  return {
    name,
    system_type: 'Custom',
    connection_type: connectionType,
    connection_config: connectionConfig,
  };
}

/** POST /api/onboard/connect body for AWS IoT SiteWise (i3X): the endpoint
 * URL rides as connection_string AND connection_config.base_url (the live
 * instance URL discover/provision prefer); the OPTIONAL bearer token rides in
 * connection_config, where the server extracts it into Secrets Manager
 * (adt/conn/<id>) — the sanitized catalog entry never carries it. */
export function buildI3xConnectBody(
  name: string,
  endpoint: string,
  token: string,
): Record<string, unknown> {
  const connection_config: Record<string, string> = {
    base_url: endpoint,
    auth_scheme: 'bearer',
  };
  if (token) connection_config.token = token;
  return {
    name,
    system_type: 'Custom',
    connection_type: 'SiteWise (i3X)',
    connection_string: endpoint,
    connection_config,
  };
}

// Tulip is a first-class connection type ("Tulip (Tables
// API)") — the base URL rides as connection_string AND connection_config.base_url,
// and the OPTIONAL raw api_key/api_secret ride in connection_config, where the
// server encodes them to an HTTP Basic token and routes it STRAIGHT to Secrets
// Manager (adt/conn/<id>) — the sanitized catalog entry never carries them. An
// OPTIONAL non-secret workspace_id scopes every Tables-API call to
// /api/v3/w/<id>/... for an instance-wide Service Account; blank ⇒ the unscoped
// path a workspace-scoped token uses (byte-identical body).
export function buildTulipConnectBody(
  name: string,
  baseUrl: string,
  apiKey: string,
  apiSecret: string,
  workspaceId = '',
): Record<string, unknown> {
  const connection_config: Record<string, string> = { base_url: baseUrl };
  const ws = (workspaceId || '').trim();
  if (ws) connection_config.workspace_id = ws;
  if (apiKey) connection_config.api_key = apiKey;
  if (apiSecret) connection_config.api_secret = apiSecret;
  return {
    name,
    // MES: the exact-agreement identity pass fails
    // closed on an unknown (Custom) class, so a Custom Tulip system's
    // cross-system joins could only ever be LLM-proposed. Admins can still
    // change the type afterwards.
    system_type: 'MES',
    connection_type: 'Tulip (Tables API)',
    connection_string: baseUrl,
    connection_config,
  };
}

// Tulip with the "Secrets Manager secret" credential source: the server reads
// the credentials from the NAMED secret (secret_arn in connection_config),
// which also pins the base URL and workspace. Base URL and workspace are
// OPTIONAL here (the secret supplies them); when filled they must match the
// secret's value or the server refuses. No api_key/api_secret are sent.
export function buildTulipArnConnectBody(
  name: string,
  secretArn: string,
  baseUrl = '',
  workspaceId = '',
): Record<string, unknown> {
  const connection_config: Record<string, string> = { secret_arn: secretArn };
  const url = (baseUrl || '').trim();
  const ws = (workspaceId || '').trim();
  if (url) connection_config.base_url = url;
  if (ws) connection_config.workspace_id = ws;
  return {
    name,
    system_type: 'MES',
    connection_type: 'Tulip (Tables API)',
    // The URL is OPTIONAL for an ARN system; send it as connection_string only
    // when supplied (the secret otherwise supplies it).
    ...(url ? { connection_string: url } : {}),
    connection_config,
  };
}

/** POST /api/onboard/connect body for PostgreSQL. The connection string
 * carries the password, so it rides ONLY in connection_config.dsn — the server
 * routes it STRAIGHT to Secrets Manager (adt/conn/<id>) and keeps just the
 * non-secret host/port/dbname on the catalog entry. Never connection_string. */
export function buildPostgresConnectBody(
  name: string,
  schema: string,
  dsn: string,
): Record<string, unknown> {
  return {
    name,
    system_type: 'Custom',
    connection_type: 'PostgreSQL',
    schema,
    connection_config: { dsn },
  };
}

/** POST /api/onboard/connect body for a URL-only connector (REST API spec URL,
 * MCP Server URL). The URL is not a secret: it rides as connection_string. An
 * MCP Server's OPTIONAL token rides in connection_config.token, which the
 * server stores in Secrets Manager (adt/conn/<id>) and never returns; a blank
 * token sends no connection_config at all. */
export function buildUrlConnectBody(
  name: string,
  connectionType: 'REST API' | 'MCP Server',
  url: string,
  token?: string,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name,
    system_type: 'Custom',
    connection_type: connectionType,
    connection_string: url,
  };
  const t = (token || '').trim();
  if (connectionType === 'MCP Server' && t) body.connection_config = { token: t };
  return body;
}

// ── Pipeline bodies ──────────────────────────────────────────────────────────

/** POST /api/onboard/preflight — {system_id} (E1 diagnostic gate, ADR-0053). */
export function buildPreflightBody(systemId: string): Record<string, unknown> {
  return { system_id: systemId };
}

/** POST /api/onboard/discover — {system_id, connection_string, connection_type,
 * connection_config} (obStart step 1; connection_config {} for
 * connection-string-based systems). */
export function buildDiscoverBody(
  systemId: string,
  connectionString: string | undefined,
  connectionType: string | undefined,
  connectionConfig: Record<string, unknown>,
): Record<string, unknown> {
  return {
    system_id: systemId,
    connection_string: connectionString,
    connection_type: connectionType,
    connection_config: connectionConfig,
  };
}

/** POST /api/onboard/map — {system_id, tables}. */
export function buildMapBody(
  systemId: string,
  tables: unknown[],
): Record<string, unknown> {
  return { system_id: systemId, tables };
}

/** POST /api/onboard/resolve — {system_id, mappings}. */
export function buildResolveBody(
  systemId: string,
  mappings: FieldMapping[],
): Record<string, unknown> {
  return { system_id: systemId, mappings };
}

/** POST /api/onboard/provision — the full obStart step-4 body. */
export function buildProvisionBody(args: {
  systemId: string;
  systemName: string;
  plant?: string;
  schema?: string;
  rules: IdentityRule[];
  mappings: FieldMapping[];
  connectionType?: string;
  connectionString?: string;
  connectionConfig: Record<string, unknown>;
  /** The Resolve job whose result the Identity Rules checkpoint showed
   * (its status answer's `resolve_job_id`). */
  resolveJobId?: string;
}): Record<string, unknown> {
  return {
    system_id: args.systemId,
    system_name: args.systemName,
    plant: args.plant || '',
    schema: args.schema || '',
    rules: args.rules,
    mappings: args.mappings,
    connection_type: args.connectionType || 'PostgreSQL',
    connection_string: args.connectionString || '',
    connection_config: args.connectionConfig,
    // These rules are the ones confirmed at the wizard's Identity Rules
    // checkpoint. The server stamps a rule as approved there only when it is
    // one the Resolve result named here listed.
    rules_from_checkpoint: true,
    resolve_job_id: args.resolveJobId || '',
  };
}

/** POST /api/onboard/verify — {system_id, schema}. */
export function buildVerifyBody(
  systemId: string,
  schema?: string,
): Record<string, unknown> {
  return { system_id: systemId, schema: schema || '' };
}

/** POST /api/onboard/credentials — {system_id, credentials}. */
export function buildCredentialsBody(
  systemId: string,
  credentials: Record<string, string>,
): Record<string, unknown> {
  return { system_id: systemId, credentials };
}

/** POST /api/onboard/remap — {remaps: [...accepted suggestions]}. */
export function buildRemapBody(
  remaps: RemapSuggestion[],
): Record<string, unknown> {
  return { remaps };
}

/** POST /api/onboard/reconcile — the system whose onboarding just finished and
 * the id of the provision run that onboarding was (its new concepts are the
 * only ones the panel compares against). */
export function buildReconcileBody(systemId: string, runId: string): Record<string, unknown> {
  return { system_id: systemId, run_id: runId };
}

// ── Spec upload (POST /api/upload/spec) ──────────────────────────────────────

/** Detect an OpenAPI spec in an attached file: JSON with an `openapi`/`swagger`
 * key, else YAML with a top-level `openapi:`/`swagger:` line (obAttachFile). */
export function detectOpenApiSpec(text: string): {
  isSpec: boolean;
  specContent: unknown;
} {
  try {
    const parsed = JSON.parse(text) as { openapi?: unknown; swagger?: unknown };
    if (parsed.openapi || parsed.swagger) {
      return { isSpec: true, specContent: parsed };
    }
  } catch {
    // Try YAML detection by looking for openapi: or swagger: at top level
    if (/^(openapi|swagger)\s*:/m.test(text)) {
      return { isSpec: true, specContent: text };
    }
  }
  return { isSpec: false, specContent: null };
}

/** Spec upload body — raw YAML string ⇒ {filename, raw_yaml}; parsed JSON ⇒
 * {filename, content}. */
export function buildSpecUploadBody(
  filename: string,
  specContent: unknown,
): Record<string, unknown> {
  return typeof specContent === 'string'
    ? { filename, raw_yaml: specContent }
    : { filename, content: specContent };
}

// ── Admin: scan budget + connection edit ─────────────────────────────────────

/** PUT /api/admin/scan-budget/{id} body — blank or non-positive input clears
 * the per-system budget (max_scanned_bytes: null). */
export function buildScanBudgetPayload(rawInput: string): ScanBudgetPayload {
  const trimmed = rawInput.trim();
  const num = trimmed === '' ? null : Number(trimmed);
  return num != null && Number.isFinite(num) && num > 0
    ? { max_scanned_bytes: Math.floor(num) }
    : { max_scanned_bytes: null };
}

/** Non-secret + secret field set per connection type (_obConnFields). The
 * secret field is write-only (blank, omitted from the PUT body unless the
 * admin types a value). */
export function obConnFields(connType: string | undefined): ConnField[] {
  const t = String(connType || '').toLowerCase();
  // Tulip is a first-class type — base URL + raw api_key/
  // api_secret (the secret field; the server encodes them to a Basic token).
  // Checked BEFORE the generic REST branch so it wins its own field set. No
  // Workspace ID here: the edit allowlist does not take it, so the scope is set
  // once, at Connect New System (re-register the system to change it).
  if (t.includes('tulip')) {
    return [
      { key: 'base_url', label: 'Base URL', secret: false, placeholder: 'https://your.tulip.co' },
      { key: 'api_key', label: 'API Key', secret: false },
      { key: 'api_secret', label: 'API Secret', secret: true },
    ];
  }
  if (t.includes('mcp')) {
    return [
      { key: 'url', label: 'MCP Server URL', secret: false, placeholder: 'https://host/mcp' },
      { key: 'token', label: 'Token', secret: true },
    ];
  }
  if (t.includes('rest')) {
    return [
      { key: 'base_url', label: 'Base URL', secret: false, placeholder: 'https://api.example.com' },
      { key: 'token', label: 'API Token', secret: true },
    ];
  }
  if (t.includes('postgres') || t.includes('sql')) {
    return [
      { key: 'host', label: 'Host', secret: false, placeholder: 'db.example.com' },
      { key: 'port', label: 'Port', secret: false, placeholder: '5432' },
      { key: 'password', label: 'Password', secret: true },
    ];
  }
  return [
    { key: 'url', label: 'Connection URL', secret: false },
    { key: 'token', label: 'Secret', secret: true },
  ];
}

/** Pre-fill non-secret fields from what the UI already knows (_obPrefill). The
 * catalog masks inline credentials to "://***@" (server-side), so we only use
 * the connection string when it carries no mask marker, and parse host/port
 * from the masked form (creds already stripped) — never a secret. */
export function obPrefill(sys: {
  connection_string?: string;
  connection_config?: Record<string, unknown>;
}): Record<string, string> {
  const cs = sys.connection_string || '';
  const out: Record<string, string> = {};
  if (cs && cs.indexOf('***') === -1) {
    out.url = cs;
    out.base_url = cs;
  }
  const m =
    cs.match(/@([^:/?@]+)(?::(\d+))?/) || cs.match(/\/\/([^:/?@]+)(?::(\d+))?/);
  if (m) {
    out.host = m[1];
    if (m[2]) out.port = m[2];
  }
  return out;
}

export interface ObFieldOption {
  value: string;
  label: string;
  disabled: boolean;
}

/** The Edit form's "Authorization header" choice for a user MCP system: how
 * its token is sent (PUT /api/admin/connection `auth_scheme`). */
export const MCP_AUTH_SCHEME_FIELD: ConnField = {
  key: 'auth_scheme',
  label: 'Authorization header',
  secret: false,
  options: ['bearer', 'api_key', 'bearer_literal'],
  optionLabels: {
    bearer: 'Bearer token',
    api_key: 'X-API-Key',
    bearer_literal: 'Token as entered',
  },
};

/** True when a catalog system's credentials come from a named Secrets Manager
 * secret. The AUTHORITATIVE source is the current-connection summary's
 * ``credential_source`` (``override``); the catalog card's own marker is only
 * the fallback before that GET answers. Never reads a secret value. */
export function isSecretSourced(sys: CatalogSystem, override?: string): boolean {
  if (override !== undefined) return override === 'secret_arn';
  const cfg = (sys.connection_config || {}) as Record<string, unknown>;
  return String(cfg.credential_source || '') === 'secret_arn';
}

/** Connection types that can take their credentials from a named Secrets
 * Manager secret (the server's SECRET_ARN_CREDENTIAL_TYPES). */
const SECRET_ARN_CONNECTION_TYPES: readonly string[] = ['Tulip (Tables API)'];

/** True when a system COULD be secret-sourced, so its Edit form must wait for
 * the server's current-connection answer before offering editable fields: its
 * catalog card already says so, or the deploy offers the secret source for
 * its type (``secretArnSourceEnabled``, the caller's per-type answer from
 * ``secretArnSourceOffered``) and its type supports it. Every other system
 * renders its form at once, as before. */
export function canBeSecretSourced(sys: CatalogSystem, secretArnSourceEnabled: boolean): boolean {
  if (isSecretSourced(sys)) return true;
  return secretArnSourceEnabled && SECRET_ARN_CONNECTION_TYPES.includes(String(sys.connection_type || ''));
}

/** The Edit form's fields for a catalog system: obConnFields, plus the
 * Authorization header select for an MCP system a user registered. HighByte
 * and the bundled demo MCP systems keep their fields unchanged. ``override`` is
 * the authoritative ``credential_source`` from the current-connection summary;
 * when it says secret-sourced the form is read-only even if the catalog card is
 * stale (lost the marker at provision/restart). */
export function obEditConnFields(sys: CatalogSystem, override?: string): ConnField[] {
  // A secret-sourced system (credentials from a named Secrets Manager secret)
  // shows its connection READ-ONLY: Base URL and Workspace from the secret, the
  // secret ARN, and NO API key / secret fields. The edit sends no routing or
  // auth fields (buildEditConnectionBody skips read-only fields); to change
  // where the key goes, rewrite the secret, not this form. Generic labels, no
  // workshop wording.
  if (isSecretSourced(sys, override)) {
    return [
      { key: 'base_url', label: 'Base URL', secret: false, readOnly: true },
      { key: 'workspace_id', label: 'Workspace ID', secret: false, readOnly: true },
      { key: 'secret_arn', label: 'Secrets Manager secret ARN', secret: false, readOnly: true },
    ];
  }
  const fields = obConnFields(sys.connection_type);
  const id = String(sys.id || sys.system_id || '');
  const userMcp =
    String(sys.connection_type || '')
      .toLowerCase()
      .includes('mcp') &&
    sys.connection_origin === 'user' &&
    id !== 'highbyte';
  return userMcp ? [...fields, MCP_AUTH_SCHEME_FIELD] : fields;
}

/** The choices a fixed-option Edit field shows. A stored value outside the
 * options (a value saved as free text before the allowlist) is listed
 * first, marked unsupported, so the form shows what is saved instead of
 * displaying the first option while Save sends something else. */
export function obFieldOptions(f: ConnField, value: string): ObFieldOption[] {
  const options = f.options || [];
  const out = options.map((o) => ({ value: o, label: f.optionLabels?.[o] ?? o, disabled: false }));
  const v = (value || '').trim();
  if (v && !options.includes(v)) {
    out.unshift({ value: v, label: `${v} (not supported)`, disabled: true });
  }
  return out;
}

/** PUT /api/admin/connection/{id} body (obSubmitEditForm): trimmed values;
 * a blank secret is OMITTED (keep current); blank non-secrets are omitted. */
export function buildEditConnectionBody(
  fields: ConnField[],
  values: Record<string, string | undefined>,
): Record<string, string> {
  const body: Record<string, string> = {};
  fields.forEach((f) => {
    if (f.readOnly) return; // read-only display field (secret-sourced) → never sent
    const val = (values[f.key] ?? '').trim();
    if (f.secret) {
      if (val) body[f.key] = val; // blank secret → omit (keep current)
    } else if (val) {
      body[f.key] = val;
    }
  });
  return body;
}

// ── PostgreSQL custom-connect helpers ────────────────────────────────────────

/** Extract the search_path schema from a PostgreSQL connection string
 * (startCustomOnboard): matches `search_path%3D<schema>` (URL-encoded) or
 * `search_path=<schema>`; '' when absent (the form then rejects the string). */
export function extractPostgresSchema(endpoint: string): string {
  const schemaMatch =
    endpoint.match(/search_path%3D(\w+)/) || endpoint.match(/search_path=(\w+)/);
  return schemaMatch ? schemaMatch[1] : '';
}
