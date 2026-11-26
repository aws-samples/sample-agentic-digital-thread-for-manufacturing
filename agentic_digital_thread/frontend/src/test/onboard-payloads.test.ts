// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboarding payload-builder tests (Task 2.4). The request-body field names are
 * the established API contract — these tests pin them, with special
 * focus on the SiteWise Direct + Athena cross-account role_arn/external_id
 * serialization (only-when-non-empty semantics). */
import { describe, expect, it } from 'vitest';

import {
  buildAthenaConnectionConfig,
  buildConnectBody,
  buildI3xConnectBody,
  buildTulipConnectBody,
  buildTulipArnConnectBody,
  buildCredentialsBody,
  buildDiscoverBody,
  buildEditConnectionBody,
  buildMapBody,
  buildProvisionBody,
  buildRemapBody,
  buildResolveBody,
  buildScanBudgetPayload,
  buildSiteWiseDirectConnectionConfig,
  buildSpecUploadBody,
  buildVerifyBody,
  confirmedMappingsFromRows,
  detectOpenApiSpec,
  extractPostgresSchema,
  isSecretSourced,
  obConnFields,
  obEditConnFields,
  obFieldOptions,
  obPrefill,
} from '../views/onboard/payloads';
import type { MappingRowVm } from '../views/onboard/flow-store';

describe('SiteWise Direct connection_config (cross-account)', () => {
  it('serializes region, role_arn and external_id EXACTLY when provided', () => {
    const cfg = buildSiteWiseDirectConnectionConfig({
      region: 'us-west-2',
      roleArn: 'arn:aws:iam::111122223333:role/SiteWiseReadOnly',
      externalId: 'adt-external-id-1',
    });
    expect(cfg).toEqual({
      region: 'us-west-2',
      role_arn: 'arn:aws:iam::111122223333:role/SiteWiseReadOnly',
      external_id: 'adt-external-id-1',
    });
  });

  it('omits ALL keys when blank (ambient account credentials)', () => {
    expect(
      buildSiteWiseDirectConnectionConfig({ region: '', roleArn: '', externalId: '' }),
    ).toEqual({});
  });

  it('includes role_arn without external_id (and vice versa) independently', () => {
    expect(
      buildSiteWiseDirectConnectionConfig({
        roleArn: 'arn:aws:iam::111122223333:role/SiteWiseReadOnly',
      }),
    ).toEqual({ role_arn: 'arn:aws:iam::111122223333:role/SiteWiseReadOnly' });
    expect(
      buildSiteWiseDirectConnectionConfig({ externalId: 'only-external' }),
    ).toEqual({ external_id: 'only-external' });
  });

  it('builds the connect body with connection_type "SiteWise (Direct)"', () => {
    const cfg = buildSiteWiseDirectConnectionConfig({
      roleArn: 'arn:aws:iam::111122223333:role/SiteWiseReadOnly',
      externalId: 'x-1',
    });
    expect(buildConnectBody('Plant Historian', 'SiteWise (Direct)', cfg)).toEqual({
      name: 'Plant Historian',
      system_type: 'Custom',
      connection_type: 'SiteWise (Direct)',
      connection_config: {
        role_arn: 'arn:aws:iam::111122223333:role/SiteWiseReadOnly',
        external_id: 'x-1',
      },
    });
  });

  it('buildI3xConnectBody: endpoint rides as connection_string AND base_url; token in config', () => {
    expect(buildI3xConnectBody('Plant i3X', 'https://i3x.example.com', 's3cret')).toEqual({
      name: 'Plant i3X',
      system_type: 'Custom',
      connection_type: 'SiteWise (i3X)',
      connection_string: 'https://i3x.example.com',
      connection_config: {
        base_url: 'https://i3x.example.com',
        auth_scheme: 'bearer',
        token: 's3cret',
      },
    });
  });

  it('buildI3xConnectBody: blank token key omitted (unauthenticated server)', () => {
    const body = buildI3xConnectBody('Plant i3X', 'https://i3x.example.com', '');
    expect(
      (body.connection_config as Record<string, string>).token,
    ).toBeUndefined();
  });

  // Tulip is a first-class connection type — base URL rides
  // as connection_string AND connection_config.base_url; the raw api_key/
  // api_secret ride in connection_config (server encodes → Basic → Secrets Mgr).
  it('buildTulipConnectBody: base URL rides as connection_string AND base_url; raw key/secret in config', () => {
    expect(
      buildTulipConnectBody('Plant Tulip', 'https://your-instance.tulip.co', 'k3y', 's3cret'),
    ).toEqual({
      name: 'Plant Tulip',
      // MES: the exact-agreement identity pass fails closed
      // on an unknown (Custom) class, so a Custom Tulip could never be joined.
      system_type: 'MES',
      connection_type: 'Tulip (Tables API)',
      connection_string: 'https://your-instance.tulip.co',
      connection_config: {
        base_url: 'https://your-instance.tulip.co',
        api_key: 'k3y',
        api_secret: 's3cret',
      },
    });
  });

  it('buildTulipConnectBody: blank api_key/api_secret keys omitted (unauthenticated instance)', () => {
    const body = buildTulipConnectBody('Plant Tulip', 'https://your-instance.tulip.co', '', '');
    const cfg = body.connection_config as Record<string, string>;
    expect(cfg.api_key).toBeUndefined();
    expect(cfg.api_secret).toBeUndefined();
    expect(cfg.base_url).toBe('https://your-instance.tulip.co');
  });

  // Optional workspace id (instance-wide Service Account) — item 1. When set it
  // rides as the non-secret connection_config.workspace_id; blank ⇒ omitted so
  // a workspace-scoped-key body stays byte-identical to before.
  it('buildTulipConnectBody: workspace_id rides in config when set', () => {
    const cfg = buildTulipConnectBody(
      'Plant Tulip', 'https://your-instance.tulip.co', 'k', 's', 'ws1',
    ).connection_config as Record<string, string>;
    expect(cfg.workspace_id).toBe('ws1');
  });

  it('buildTulipConnectBody: blank workspace_id is omitted (byte-identical)', () => {
    const cfg = buildTulipConnectBody(
      'Plant Tulip', 'https://your-instance.tulip.co', 'k', 's', '',
    ).connection_config as Record<string, string>;
    expect(cfg.workspace_id).toBeUndefined();
    // default arg (omitted) behaves the same
    const cfg2 = buildTulipConnectBody(
      'Plant Tulip', 'https://your-instance.tulip.co', 'k', 's',
    ).connection_config as Record<string, string>;
    expect(cfg2.workspace_id).toBeUndefined();
  });

  // Secrets Manager secret credential source (item 2). The ARN rides in
  // connection_config.secret_arn; base_url/workspace_id are OPTIONAL (the
  // named secret supplies them); no api_key/api_secret are ever sent.
  it('buildTulipArnConnectBody: ARN only (url/workspace from the secret)', () => {
    const arn = 'arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf';
    const body = buildTulipArnConnectBody('Plant Tulip', arn);
    expect(body).toEqual({
      name: 'Plant Tulip',
      system_type: 'MES',
      connection_type: 'Tulip (Tables API)',
      connection_config: { secret_arn: arn },
    });
    const cfg = body.connection_config as Record<string, string>;
    expect(cfg.api_key).toBeUndefined();
    expect(cfg.api_secret).toBeUndefined();
    expect('connection_string' in body).toBe(false);
  });

  it('buildTulipArnConnectBody: optional url + workspace ride along when filled', () => {
    const arn = 'arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf';
    const body = buildTulipArnConnectBody(
      'Plant Tulip', arn, 'https://acme.tulip.co', 'ws-7');
    expect(body.connection_string).toBe('https://acme.tulip.co');
    const cfg = body.connection_config as Record<string, string>;
    expect(cfg.base_url).toBe('https://acme.tulip.co');
    expect(cfg.workspace_id).toBe('ws-7');
    expect(cfg.secret_arn).toBe(arn);
  });
});

describe('Athena connection_config', () => {
  it('carries workgroup/database/catalog plus optional keys exactly', () => {
    const cfg = buildAthenaConnectionConfig({
      workgroup: 'primary',
      database: 'my_glue_db',
      catalog: '',
      region: 'us-east-1',
      outputLocation: 's3://amzn-s3-demo-athena-results/',
      roleArn: 'arn:aws:iam::444455556666:role/AthenaReadOnly',
      externalId: 'ext-42',
    });
    expect(cfg).toEqual({
      workgroup: 'primary',
      database: 'my_glue_db',
      catalog: 'AwsDataCatalog', // blank catalog defaults
      region: 'us-east-1',
      output_location: 's3://amzn-s3-demo-athena-results/',
      role_arn: 'arn:aws:iam::444455556666:role/AthenaReadOnly',
      external_id: 'ext-42',
    });
  });

  it('omits optional keys when blank; keeps required + catalog default', () => {
    expect(
      buildAthenaConnectionConfig({ workgroup: 'wg', database: 'db' }),
    ).toEqual({ workgroup: 'wg', database: 'db', catalog: 'AwsDataCatalog' });
  });

  it('builds the Athena connect body', () => {
    expect(
      buildConnectBody('LIMS', 'Athena', { workgroup: 'wg', database: 'db', catalog: 'AwsDataCatalog' }),
    ).toEqual({
      name: 'LIMS',
      system_type: 'Custom',
      connection_type: 'Athena',
      connection_config: { workgroup: 'wg', database: 'db', catalog: 'AwsDataCatalog' },
    });
  });
});

describe('pipeline bodies', () => {
  it('discover: {system_id, connection_string, connection_type, connection_config}', () => {
    expect(
      buildDiscoverBody('cmms', 'postgresql://h/db', 'PostgreSQL', {}),
    ).toEqual({
      system_id: 'cmms',
      connection_string: 'postgresql://h/db',
      connection_type: 'PostgreSQL',
      connection_config: {},
    });
  });

  it('map: {system_id, tables}; resolve: {system_id, mappings}', () => {
    const tables = [{ name: 'work_orders', row_count: 3 }];
    expect(buildMapBody('cmms', tables)).toEqual({ system_id: 'cmms', tables });
    const mappings = [{ field: 'work_orders.wo_number', concept: 'work-order' }];
    expect(buildResolveBody('cmms', mappings)).toEqual({
      system_id: 'cmms',
      mappings,
    });
  });

  it('provision: full body with defaults for missing plant/schema/type/string', () => {
    expect(
      buildProvisionBody({
        systemId: 'sw1',
        systemName: 'SiteWise',
        rules: [],
        mappings: [],
        connectionConfig: { role_arn: 'arn:aws:iam::111122223333:role/R' },
        connectionType: 'SiteWise (Direct)',
      }),
    ).toEqual({
      system_id: 'sw1',
      system_name: 'SiteWise',
      plant: '',
      schema: '',
      rules: [],
      mappings: [],
      connection_type: 'SiteWise (Direct)',
      connection_string: '',
      connection_config: { role_arn: 'arn:aws:iam::111122223333:role/R' },
      // The wizard's rules are the ones confirmed at its Identity Rules
      // checkpoint; the server stamps a rule as approved there only on this
      // and only when it is in the Resolve result named below.
      rules_from_checkpoint: true,
      resolve_job_id: '',
    });
    // connection_type falls back to PostgreSQL exactly like obStart
    expect(
      buildProvisionBody({
        systemId: 'x',
        systemName: 'X',
        rules: [],
        mappings: [],
        connectionConfig: {},
      }).connection_type,
    ).toBe('PostgreSQL');
  });

  it('provision: names the Resolve job whose rules the checkpoint showed', () => {
    expect(
      buildProvisionBody({
        systemId: 'x',
        systemName: 'X',
        rules: [],
        mappings: [],
        connectionConfig: {},
        resolveJobId: 'resolve-job',
      }).resolve_job_id,
    ).toBe('resolve-job');
  });

  it('verify / credentials / remap bodies', () => {
    expect(buildVerifyBody('cmms', 'maint')).toEqual({
      system_id: 'cmms',
      schema: 'maint',
    });
    expect(buildVerifyBody('cmms')).toEqual({ system_id: 'cmms', schema: '' });
    expect(
      buildCredentialsBody('c1', { connection_string: 'pg://x', schema: 's' }),
    ).toEqual({
      system_id: 'c1',
      credentials: { connection_string: 'pg://x', schema: 's' },
    });
    const remaps = [
      { field_id: 'f1', concept_id: 'batch', role: 'identifier', confidence: 0.9, old_concept: 'unknown' },
    ];
    expect(buildRemapBody(remaps)).toEqual({ remaps });
  });
});

describe('scan-budget payload', () => {
  it('positive number → floored bytes', () => {
    expect(buildScanBudgetPayload('1048576')).toEqual({ max_scanned_bytes: 1048576 });
    expect(buildScanBudgetPayload(' 1500.7 ')).toEqual({ max_scanned_bytes: 1500 });
  });
  it('blank / zero / negative / NaN → null (clear the per-system budget)', () => {
    expect(buildScanBudgetPayload('')).toEqual({ max_scanned_bytes: null });
    expect(buildScanBudgetPayload('0')).toEqual({ max_scanned_bytes: null });
    expect(buildScanBudgetPayload('-5')).toEqual({ max_scanned_bytes: null });
    expect(buildScanBudgetPayload('abc')).toEqual({ max_scanned_bytes: null });
  });
});

describe('admin edit-connection fields + body', () => {
  it('field sets per connection type', () => {
    expect(obConnFields('MCP Server').map((f) => f.key)).toEqual(['url', 'token']);
    expect(obConnFields('REST API').map((f) => f.key)).toEqual(['base_url', 'token']);
    // A fixed-choice field: a value saved as free text before the allowlist
    // is shown as it is, marked unsupported, instead of the select displaying
    // the first option while Save sends the stored value back (and gets a 400).
    const variant = { key: 'variant', label: 'Variant', secret: false, options: ['alpha', 'beta'] };
    expect(obFieldOptions(variant, '').map((o) => o.value)).toEqual(['alpha', 'beta']);
    expect(obFieldOptions(variant, 'beta').map((o) => o.value)).toEqual(['alpha', 'beta']);
    expect(obFieldOptions(variant, 'BETA')).toEqual([
      { value: 'BETA', label: 'BETA (not supported)', disabled: true },
      { value: 'alpha', label: 'alpha', disabled: false },
      { value: 'beta', label: 'beta', disabled: false },
    ]);
    // Padding is not a different value.
    expect(obFieldOptions(variant, ' beta ').map((o) => o.value)).toEqual(['alpha', 'beta']);
    // Tulip is a first-class type with its own field set,
    // matched by name BEFORE the generic REST branch.
    // No Workspace ID on Edit: the server's edit allowlist does not take it,
    // so the scope is set when the system is registered (Connect New System).
    expect(obConnFields('Tulip (Tables API)').map((f) => f.key)).toEqual([
      'base_url',
      'api_key',
      'api_secret',
    ]);
    expect(obPrefill({ connection_config: { base_url: 'https://h', workspace_id: 'ws1' } }))
      .not.toHaveProperty('workspace_id');
    expect(obConnFields('PostgreSQL').map((f) => f.key)).toEqual([
      'host',
      'port',
      'password',
    ]);
    expect(obConnFields('SiteWise (Direct)').map((f) => f.key)).toEqual([
      'url',
      'token',
    ]);
  });

  it('blank secret is OMITTED (keep current); values are trimmed', () => {
    const fields = obConnFields('PostgreSQL');
    expect(
      buildEditConnectionBody(fields, { host: ' db.example.com ', port: '5432', password: '' }),
    ).toEqual({ host: 'db.example.com', port: '5432' });
    expect(
      buildEditConnectionBody(fields, { host: 'h', password: 's3cret' }),
    ).toEqual({ host: 'h', password: 's3cret' });
  });

  it('ARN-sourced Tulip: edit form is read-only base_url/workspace/ARN, no secret fields', () => {
    const sys = {
      id: 'tulip-mes',
      name: 'Tulip MES',
      connection_type: 'Tulip (Tables API)',
      connection_origin: 'user',
      secret_arn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf',
      connection_config: { credential_source: 'secret_arn', base_url: 'https://acme.tulip.co' },
    };
    expect(isSecretSourced(sys)).toBe(true);
    const fields = obEditConnFields(sys);
    // Only read-only display fields: base_url, workspace_id, the ARN. No
    // api_key / api_secret / token fields at all.
    expect(fields.map((f) => f.key)).toEqual(['base_url', 'workspace_id', 'secret_arn']);
    expect(fields.every((f) => f.readOnly === true && f.secret === false)).toBe(true);
    // Generic labels, no workshop/seat/event wording.
    const labels = fields.map((f) => f.label.toLowerCase()).join(' ');
    for (const banned of ['workshop', 'seat', 'event']) {
      expect(labels.includes(banned)).toBe(false);
    }
  });

  it('ARN-sourced Tulip: a re-save sends NO routing or auth fields', () => {
    const sys = {
      id: 'tulip-mes',
      name: 'Tulip MES',
      connection_type: 'Tulip (Tables API)',
      connection_origin: 'user',
      secret_arn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf',
      connection_config: { credential_source: 'secret_arn' },
    };
    const fields = obEditConnFields(sys);
    // Even with the read-only values populated (as the form seeds them from the
    // current-connection summary), the body is empty: nothing routing/auth is sent.
    const body = buildEditConnectionBody(fields, {
      base_url: 'https://acme.tulip.co',
      workspace_id: 'wsX',
      secret_arn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf',
    });
    expect(body).toEqual({});
  });

  it('a non-secret-sourced Tulip keeps the editable key/secret form', () => {
    const sys = { id: 't2', name: 'Inline Tulip', connection_type: 'Tulip (Tables API)',
                  connection_origin: 'user', connection_config: {} };
    expect(isSecretSourced(sys)).toBe(false);
    expect(obEditConnFields(sys).map((f) => f.key)).toEqual(['base_url', 'api_key', 'api_secret']);
  });

  it('after restart a STALE catalog card still picks the read-only form from the summary', () => {
    // The restart case: the catalog card lost credential_source (provision/
    // restart dropped it before the backfill), so the card looks inline, but
    // the authoritative current-connection summary says credential_source=
    // secret_arn. The form must use the summary override, not the card.
    const staleCard = {
      id: 'tulip-mes',
      name: 'Tulip MES',
      connection_type: 'Tulip (Tables API)',
      connection_origin: 'user',
      connection_config: {}, // marker missing on the card
    };
    expect(isSecretSourced(staleCard)).toBe(false); // the card alone looks inline
    // The summary override is authoritative.
    expect(isSecretSourced(staleCard, 'secret_arn')).toBe(true);
    const fields = obEditConnFields(staleCard, 'secret_arn');
    expect(fields.map((f) => f.key)).toEqual(['base_url', 'workspace_id', 'secret_arn']);
    expect(fields.every((f) => f.readOnly === true)).toBe(true);
    // A re-save with the seeded read-only values sends an empty body.
    expect(
      buildEditConnectionBody(fields, {
        base_url: 'https://acme.tulip.co',
        workspace_id: 'wsX',
        secret_arn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf',
      }),
    ).toEqual({});
    // An empty summary override (a genuinely inline system) keeps the editor.
    expect(obEditConnFields(staleCard, '').map((f) => f.key)).toEqual([
      'base_url', 'api_key', 'api_secret',
    ]);
  });

  it('prefill: skips masked connection strings, parses host/port from mask', () => {
    expect(obPrefill({ connection_string: 'postgresql://user:***@db.host:5433/x' })).toEqual(
      { host: 'db.host', port: '5433' },
    );
    expect(obPrefill({ connection_string: 'https://api.example.com' })).toEqual({
      url: 'https://api.example.com',
      base_url: 'https://api.example.com',
      host: 'api.example.com',
    });
    expect(obPrefill({})).toEqual({});
  });

});

describe('spec upload + postgres schema extraction', () => {
  it('detects JSON OpenAPI specs → {filename, content}', () => {
    const text = JSON.stringify({ openapi: '3.0.0', paths: {} });
    const { isSpec, specContent } = detectOpenApiSpec(text);
    expect(isSpec).toBe(true);
    expect(buildSpecUploadBody('api.json', specContent)).toEqual({
      filename: 'api.json',
      content: { openapi: '3.0.0', paths: {} },
    });
  });

  it('detects YAML specs → {filename, raw_yaml}; rejects non-specs', () => {
    const yaml = 'openapi: 3.0.0\npaths: {}\n';
    const det = detectOpenApiSpec(yaml);
    expect(det.isSpec).toBe(true);
    expect(buildSpecUploadBody('api.yaml', det.specContent)).toEqual({
      filename: 'api.yaml',
      raw_yaml: yaml,
    });
    expect(detectOpenApiSpec('just some text').isSpec).toBe(false);
    expect(detectOpenApiSpec('{"info":{}}').isSpec).toBe(false);
  });

  it('extracts search_path schema (encoded and plain), else empty', () => {
    expect(
      extractPostgresSchema(
        'postgresql://u:p@h:5432/db?options=-csearch_path%3Dmaint',
      ),
    ).toBe('maint');
    expect(
      extractPostgresSchema('postgresql://u:p@h:5432/db?options=-csearch_path=erp'),
    ).toBe('erp');
    expect(extractPostgresSchema('postgresql://u:p@h:5432/db')).toBe('');
  });
});

// ── React-state payload derivations (jsx-migration W5.1) ─────────────────────
// The mapping-checkpoint confirmation and the provision collectors now read
// store view-models — these pin the state→payload transforms that replaced
// the DOM scraping.

describe('confirmedMappingsFromRows (mapping checkpoint React state)', () => {
  const mappings = [
    { field: 'a.wo', concept: 'work-order', confidence: 0.9 },
    { field: 'a.junk', concept: 'needs_review', confidence: 0 },
    { field: 'a.batch', concept: 'batch', confidence: 0.8 },
  ];

  it('reads tags from rows: skip tags dropped, first tag becomes primary', () => {
    const rows: MappingRowVm[] = [
      {
        field: 'a.wo',
        review: false,
        tags: [{ concept: 'work-order', confidence: 0.9 }],
      },
      {
        field: 'a.junk',
        review: false,
        tags: [{ concept: 'skip', confidence: 0, skip: true }],
      },
      {
        field: 'a.batch',
        review: false,
        tags: [
          { concept: 'material', confidence: 0.5, manual: true },
          { concept: 'batch', confidence: 0.8 },
        ],
      },
    ];
    const out = confirmedMappingsFromRows(mappings, rows);
    expect(out[0]).toMatchObject({
      concept: 'work-order',
      confidence: 0.9,
      concepts: [{ concept: 'work-order', confidence: 0.9 }],
    });
    // skipped row → no tags → needs_review fallback (legacy DOM behavior)
    expect(out[1]).toMatchObject({ concept: 'needs_review', confidence: 0, concepts: [] });
    // manual pick added first wins as primary
    expect(out[2]).toMatchObject({
      concept: 'material',
      confidence: 0.5,
      concepts: [
        { concept: 'material', confidence: 0.5 },
        { concept: 'batch', confidence: 0.8 },
      ],
    });
  });

  it('null rows (no state) falls back to the mapping concepts', () => {
    const out = confirmedMappingsFromRows(mappings, null);
    expect(out[0].concept).toBe('work-order');
    expect(out[1].concept).toBe('needs_review');
  });
});
