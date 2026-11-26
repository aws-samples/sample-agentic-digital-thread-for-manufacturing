// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ObModals/> (jsx-migration W5.1) — the onboard overlays, store-driven:
 * cancel-onboarding confirm, Reset All Data, Disconnect <system>, and the
 * Connect New System form (whose submit reads REACT STATE field values into
 * startCustomOnboard — never the DOM).
 * so a lost dismiss click can never strand it open across visits. */
import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ApiError } from '../../../api/client';

import { startCustomOnboard, type ConnectNewValues } from '../catalog';
import { secretArnSourceOffered } from '../state';
import { catalogStore, setConnectNewOpen } from '../catalog-store';
import { useStore } from '../../../lib/store';
import {
  obCancelConfirmed,
  obDisconnectConfirmed,
  obResetAllConfirmed,
  obRetryStillNeeded,
} from '../flow';
import { closeModal, flowStore } from '../flow-store';
import type { WorkshopMode } from '../../../features/workshop/clock';
import { ModalSurface } from '../../../shared/ui/ModalSurface';
import type { DialogFocusTarget } from '../../../shared/ui/useDialogFocus';

/** Shared onboard-overlay surface: an accessible role="dialog" (ModalSurface +
 * useDialogFocus) that labels itself by a heading id (``labelledBy``) or an
 * invisible ``label``, moves initial focus to ``initialFocus``, traps Tab,
 * closes on Escape and overlay click, and returns focus to the opener on
 * close. Every onboard modal renders through it, so the ordinary removal
 * dialog gets the same a11y as the registration one. */
function Overlay({
  children,
  onClose,
  extraClass,
  labelledBy,
  label,
  initialFocus,
}: {
  children: React.ReactNode;
  onClose: () => void;
  extraClass?: string;
  labelledBy?: string;
  label?: string;
  initialFocus?: DialogFocusTarget;
}) {
  const labelProps = labelledBy
    ? ({ labelledBy } as const)
    : ({ label: label ?? 'Dialog' } as const);
  return (
    <ModalSurface
      open
      onClose={onClose}
      closeOnOverlayClick
      initialFocus={initialFocus}
      className={'ob-modal-overlay' + (extraClass ? ' ' + extraClass : '')}
      {...labelProps}
    >
      {children}
    </ModalSurface>
  );
}

const modalBtnRow: React.CSSProperties = {
  display: 'flex',
  gap: '0.5rem',
  justifyContent: 'flex-end',
};

const modalText: React.CSSProperties = {
  margin: '0 0 1rem',
  color: 'var(--muted)',
  fontSize: '0.85rem',
};

function CancelModal({ then }: { then?: () => void }) {
  return (
    <Overlay onClose={closeModal} label="Cancel onboarding">
      <div className="ob-modal">
        <h4 style={{ margin: '0 0 0.5rem' }}>Cancel onboarding?</h4>
        <p style={modalText}>
          This will cancel the current system onboarding in progress.
        </p>
        <div style={modalBtnRow}>
          <button className="ob-modal-btn secondary" id="ob-modal-no" onClick={closeModal}>
            Keep going
          </button>
          <button
            className="ob-modal-btn danger"
            id="ob-modal-yes"
            onClick={() => obCancelConfirmed(then)}
          >
            Cancel onboarding
          </button>
        </div>
      </div>
    </Overlay>
  );
}

/** The notice for a Reset All whose reset ran but whose reseed failed (the
 * seed function raised, a seed file failed, or the invoke failed): the
 * server's public error text (a fixed message and a reference) when it sent
 * one. Null for any other failure. */
function reseedFailureNotice(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.url !== '/api/onboard/reseed') return null;
  const notice = 'The systems were reset, but the sample databases were not reseeded.';
  const error = (err.payload as { error?: unknown } | null | undefined)?.error;
  return typeof error === 'string' && error.trim() ? `${notice} ${error.trim()}` : notice;
}

function ResetAllModal() {
  const [label, setLabel] = useState('Reset All Data');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const confirm = async () => {
    setBusy(true);
    setNotice(null);
    setLabel('Resetting...');
    try {
      await obResetAllConfirmed(setLabel);
    } catch (err) {
      // The reset runs as a server job the confirm polls (flow.ts runResetAll).
      // 503 = it did not start, or it stopped BEFORE wiping anything because
      // runtimes were still deleting (or its lease was lost); the reset is safe
      // to simply retry. A 503 whose body carries reason 'startup_restore' is
      // the app still restoring its onboarded systems after a restart: nothing
      // was started, and the reset works once that ends. 409 = a system's
      // removal (or another reset) is running; nothing was started.
      const resetRefused = err instanceof ApiError && err.url === '/api/onboard/reset';
      const restoring =
        resetRefused &&
        (err.payload as { reason?: unknown } | null | undefined)?.reason === 'startup_restore';
      setLabel(
        restoring
          ? 'Still restoring after a restart, retry in a minute'
          : resetRefused && err.status === 503
            ? 'Still deleting — retry in a minute'
            : resetRefused && err.status === 409
              ? 'A removal is running — retry when it finishes'
              : 'Failed',
      );
      setNotice(reseedFailureNotice(err));
      setBusy(false);
    }
  };

  return (
    <Overlay onClose={closeModal} label="Reset all data">
      <div className="ob-modal">
        <h4 style={{ margin: '0 0 0.5rem' }}>Reset all data?</h4>
        <p style={modalText}>
          This disconnects all systems and clears everything the platform
          learned about them from the knowledge graph. Systems stay registered
          so they can be onboarded again. On deployments with bundled sample
          data, the sample databases are also reseeded to a clean state.
        </p>
        {notice ? (
          <p className="ob-reset-error" role="alert">
            {notice}
          </p>
        ) : null}
        <div style={modalBtnRow}>
          <button className="ob-modal-btn secondary" id="ob-reset-no" onClick={closeModal}>
            Cancel
          </button>
          <button
            className="ob-modal-btn danger"
            id="ob-reset-yes"
            disabled={busy}
            onClick={() => void confirm()}
          >
            {label}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

function DisconnectModal({
  systemId,
  systemName,
  registration = false,
  retryFull,
  retry = false,
  workshopMode,
  userOrigin = false,
}: {
  systemId: string;
  systemName?: string;
  registration?: boolean;
  /** OnboardView's useWorkshopMode() reading. For a system the user did not
   * connect (``userOrigin`` false), the "also forget" checkbox is offered
   * only when the account is known not to run a workshop story ('off'): an
   * attendee who forgot a seeded system's registration could not bring it
   * back, and the server refuses that removal there. A system the user
   * connected can be connected again, so it keeps the checkbox. */
  workshopMode: WorkshopMode;
  /** The system's catalog connection_origin is 'user'. */
  userOrigin?: boolean;
  /** The mode of a removal that did not finish ("Remove again"): the retry
   * must finish it the same way, so the "also forget" choice is fixed to it.
   * Undefined for a fresh removal. */
  retryFull?: boolean;
  /** Opened by "Remove again": before it confirms, the system is re-read
   * from the server and nothing is removed unless it is still half-removed. */
  retry?: boolean;
}) {
  // A registration-only removal (a 'registered' user system that was never
  // onboarded, or returned to 'registered' by a default removal) is ALWAYS a
  // full removal: forgetting the registration and scheduling its own secret
  // for deletion is the whole point, so the "also forget" choice is forced on
  // and the checkbox is not shown.
  const [label, setLabel] = useState(registration ? 'Remove registration' : 'Remove system');
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState('');
  const [forget, setForget] = useState(registration || retryFull === true);
  const [stateNote, setStateNote] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const modeFixed = retryFull !== undefined;
  // Whether the "also forget" choice is offered right now: the workshop mode
  // can turn on while the dialog is open, hiding a box already checked.
  const forgetOffered = workshopMode === 'off' || userOrigin;
  // What is sent, from the current props: a registration-only removal and a
  // retry of a known full removal stay full; otherwise the box counts only
  // while it is offered.
  const submitFull = registration || retryFull === true || (forget && forgetOffered);
  // What the dialog says about the registration: "by default" only when the
  // "also forget" box offers another choice; with no box, plainly that it is
  // kept; nothing when the removal forgets it (the mode note says so).
  const returnsText = 'so the system returns to the catalog ready to onboard again. ';
  const keptText = forgetOffered
    ? `By default the connection registration is kept, ${returnsText}`
    : submitFull
      ? ''
      : `The connection registration is kept, ${returnsText}`;
  const armed = typed.trim() === systemId;
  const confirmRef = useRef<HTMLInputElement>(null);
  const headingId = 'ob-disc-heading';

  const confirm = async () => {
    if (!armed || stale) return;
    setBusy(true);
    // A "Remove again" dialog: this tab's view can be stale, so the state is
    // read again before posting, and the POST is marked as a retry so the
    // server refuses it if the system recovered after this read.
    const showChanged = (check: 'changed' | 'unknown', status?: string) => {
      setStateNote(
        check === 'changed'
          ? `Its state changed: it is ${status} now, so this removal no longer ` +
              'applies. Nothing was removed. Close this and use its card.'
          : 'Could not check its current state, so nothing was removed. Try again.',
      );
      setStale(check === 'changed');
      setLabel(registration ? 'Remove registration' : 'Remove system');
      setBusy(false);
    };
    if (retry) {
      setLabel('Checking its current state...');
      const fresh = await obRetryStillNeeded(systemId);
      if (fresh.check !== 'yes') {
        showChanged(fresh.check, fresh.status);
        return;
      }
      setStateNote(null);
    }
    setLabel('Starting removal...');
    try {
      const err = await obDisconnectConfirmed(
        systemId,
        submitFull,
        registration,
        retry,
      );
      if (err) {
        if (retry) {
          // Refused: if the server says the system is no longer half-removed,
          // show its current state instead of a retryable error.
          const after = await obRetryStillNeeded(systemId);
          if (after.check === 'changed') {
            showChanged('changed', after.status);
            return;
          }
        }
        setLabel(err);
        setBusy(false);
      }
    } catch {
      setLabel('Failed');
      setBusy(false);
    }
  };

  return (
    <Overlay onClose={closeModal} labelledBy={headingId} initialFocus={confirmRef}>
      <div className="ob-modal">
        <h4 id={headingId} style={{ margin: '0 0 0.5rem' }}>
          {registration ? 'Remove registration' : 'Remove'} {systemName || systemId}?
        </h4>
        {registration ? (
          <p style={modalText}>
            This forgets the connection registration: the system disappears from
            the catalog and must be re-registered by hand. For a system you
            connected yourself, its stored credentials are scheduled for
            deletion after 7 days. It is not currently onboarded, so there is no
            knowledge-graph data or agent runtime to remove.
          </p>
        ) : (
          <p style={modalText}>
            This removes everything the platform learned about the system: its
            knowledge-graph node and edges (schemas, field mappings, join
            paths) and its agent runtime. Source data in the system itself is
            not touched. {keptText}Removal cascades through several services and can take a few minutes — the
            system card stays disabled until it completes.
          </p>
        )}
        {!registration && forgetOffered && (
          <label
            style={{ ...modalText, display: 'flex', gap: '0.4rem',
                     alignItems: 'flex-start', marginTop: '0.5rem' }}
          >
            <input
              id="ob-disc-forget"
              type="checkbox"
              checked={forget}
              disabled={modeFixed}
              aria-describedby={modeFixed ? 'ob-disc-mode-note' : undefined}
              onChange={(e) => setForget(e.target.checked)}
              style={{ marginTop: '0.15rem' }}
            />
            <span>
              Also forget the connection registration (the system disappears
              from the catalog and must be re-registered by hand; for a system
              you connected yourself, its stored credentials are scheduled for
              deletion after 7 days)
            </span>
          </label>
        )}
        {!registration && modeFixed && (
          <p id="ob-disc-mode-note" style={{ ...modalText, marginTop: '0.5rem' }}>
            {retryFull
              ? 'The earlier removal also forgot the registration and did not finish, so this removal forgets it too.'
              : 'The earlier removal kept the registration and did not finish, so this removal keeps it too.'}
          </p>
        )}
        <label
          htmlFor="ob-disc-confirm-input"
          style={{ ...modalText, marginTop: '0.5rem', display: 'block' }}
        >
          Type <strong>{systemId}</strong> to confirm:
        </label>
        <input
          id="ob-disc-confirm-input"
          ref={confirmRef}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={systemId}
          autoComplete="off"
          style={{
            width: '100%',
            padding: '0.4rem 0.5rem',
            marginBottom: '0.75rem',
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            color: 'var(--text)',
          }}
        />
        {stateNote && (
          <p
            id="ob-disc-state-changed"
            role="status"
            style={{ ...modalText, marginTop: '0.5rem', color: 'var(--danger, #c0392b)' }}
          >
            {stateNote}
          </p>
        )}
        <div style={modalBtnRow}>
          <button className="ob-modal-btn secondary" id="ob-disc-no" onClick={closeModal}>
            Cancel
          </button>
          <button
            className="ob-modal-btn danger"
            id="ob-disc-yes"
            disabled={busy || !armed || stale}
            onClick={() => void confirm()}
          >
            {label}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

// ── Connect New System ────────────────────────────────────────────────────────

const cnLabel: React.CSSProperties = {
  display: 'block',
  fontSize: '0.75rem',
  color: 'var(--muted)',
  marginBottom: '0.25rem',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
};

const cnInput: React.CSSProperties = {
  width: '100%',
  padding: '0.5rem',
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  boxSizing: 'border-box',
};

const cnMono: React.CSSProperties = {
  ...cnInput,
  fontFamily: 'monospace',
  fontSize: '0.8rem',
};

const cnHint: React.CSSProperties = {
  fontSize: '0.7rem',
  color: 'var(--muted)',
  marginTop: '0.25rem',
};

const EMPTY_VALUES: ConnectNewValues = {
  connType: 'postgresql',
  name: '',
  plant: '',
  endpoint: '',
  mcpToken: '',
  athena: {
    workgroup: '',
    database: '',
    catalog: '',
    region: '',
    output: '',
    roleArn: '',
    externalId: '',
  },
  sitewise: { region: '', roleArn: '', externalId: '' },
  i3x: { endpoint: '', token: '' },
  tulip: {
    credentialMode: 'inline',
    baseUrl: '',
    workspaceId: '',
    apiKey: '',
    apiSecret: '',
    secretArn: '',
  },
};

function ConnectNewModal() {
  const [v, setV] = useState<ConnectNewValues>(EMPTY_VALUES);
  // The form stays mounted (keeping v) until registration succeeds; a
  // failure or validation problem shows here instead of in a toast.
  const connectNewBusy = useStore(catalogStore, (s) => s.connectNewBusy);
  const connectNewError = useStore(catalogStore, (s) => s.connectNewError);
  const close = () => setConnectNewOpen(false);

  const patchAthena = (key: keyof ConnectNewValues['athena'], value: string) =>
    setV((p) => ({ ...p, athena: { ...p.athena, [key]: value } }));
  const patchSitewise = (key: keyof ConnectNewValues['sitewise'], value: string) =>
    setV((p) => ({ ...p, sitewise: { ...p.sitewise, [key]: value } }));
  const patchI3x = (key: keyof ConnectNewValues['i3x'], value: string) =>
    setV((p) => ({ ...p, i3x: { ...p.i3x, [key]: value } }));
  // Tulip (Tables API) field patcher
  const patchTulip = (key: keyof ConnectNewValues['tulip'], value: string) =>
    setV((p) => ({ ...p, tulip: { ...p.tulip, [key]: value } }));

  const req = <span style={{ color: 'var(--red,#f87171)' }}>*</span>;

  const athenaField = (
    key: keyof ConnectNewValues['athena'],
    label: string,
    ph: string,
    required: boolean,
  ) => (
    <span key={key}>
      <label style={cnLabel}>
        {label} {required && req}
      </label>
      <input
        id={`athena-${key}`}
        placeholder={ph}
        style={cnMono}
        value={v.athena[key]}
        onChange={(e) => patchAthena(key, e.target.value)}
      />
    </span>
  );

  return (
    <Overlay onClose={close} extraClass="ob-connect-overlay" label="Connect New System">
      <div className="ob-modal ob-connect-modal">
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: '1rem',
          }}
        >
          <h3 style={{ margin: 0 }}>Connect New System</h3>
          <button
            onClick={close}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--muted)',
              fontSize: '1.2rem',
              cursor: 'pointer',
              padding: '0.25rem',
            }}
          >
            ✕
          </button>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <div>
            <label style={cnLabel}>Connection Type</label>
            <select
              id="new-conn-type"
              style={cnInput}
              value={v.connType}
              onChange={(e) => setV((p) => ({ ...p, connType: e.target.value }))}
            >
              <option value="postgresql">PostgreSQL</option>
              {/* Generic REST provisioning is not implemented (the generic
                  rest_vendor_registry adapter raises RestVendorUnsupported), so
                  the server refuses REST API registration — not offered here. */}
              <option value="rest-api" disabled>
                REST API (OpenAPI), not supported yet
              </option>
              <option value="mcp-server">MCP Server</option>
              <option value="athena">Amazon Athena</option>
              <option value="sitewise-direct">AWS IoT SiteWise (Direct)</option>
              <option value="sitewise-i3x">AWS IoT SiteWise (i3X)</option>
              {/* Tulip is a first-class connection type */}
              <option value="tulip">Tulip (Tables API)</option>
            </select>
          </div>
          <div>
            <label style={cnLabel}>System Name</label>
            <input
              id="new-conn-name"
              placeholder="e.g. Quality Lab LIMS"
              style={cnInput}
              value={v.name}
              onChange={(e) => setV((p) => ({ ...p, name: e.target.value }))}
            />
          </div>
          <div>
            <label style={cnLabel} htmlFor="new-conn-plant">
              Plant (optional)
            </label>
            <input
              id="new-conn-plant"
              placeholder="e.g. North Plant, or All Sites"
              maxLength={80}
              style={cnInput}
              value={v.plant ?? ''}
              onChange={(e) => setV((p) => ({ ...p, plant: e.target.value }))}
            />
            <div style={cnHint}>
              The site this system serves, spelled the way your other systems name it. Leave it blank
              if the system isn&apos;t tied to one site.
            </div>
          </div>
          <div id="conn-details">
            {v.connType === 'postgresql' && (
              <>
                <label style={cnLabel}>Connection String</label>
                <input
                  id="new-conn-endpoint"
                  type="password"
                  placeholder="postgresql://user:pass@host:5432/db?options=-csearch_path%3Dschema"
                  style={cnMono}
                  value={v.endpoint}
                  onChange={(e) => setV((p) => ({ ...p, endpoint: e.target.value }))}
                />
              </>
            )}
            {v.connType === 'rest-api' && (
              <>
                <label style={cnLabel}>OpenAPI Spec URL</label>
                <input
                  id="new-conn-endpoint"
                  placeholder="https://api.example.com/openapi.json"
                  style={cnMono}
                  value={v.endpoint}
                  onChange={(e) => setV((p) => ({ ...p, endpoint: e.target.value }))}
                />
                <div style={cnHint}>
                  Provide the URL to an OpenAPI/Swagger spec. The system will be
                  discovered from the API definition.
                </div>
              </>
            )}
            {v.connType === 'mcp-server' && (
              <>
                <label style={cnLabel}>MCP Server URL</label>
                <input
                  id="new-conn-endpoint"
                  placeholder="http://localhost:8200/mcp"
                  style={cnMono}
                  value={v.endpoint}
                  onChange={(e) => setV((p) => ({ ...p, endpoint: e.target.value }))}
                />
                <label style={cnLabel}>Token (optional)</label>
                <input
                  id="new-conn-token"
                  type="password"
                  autoComplete="new-password"
                  placeholder="required when the server enforces auth"
                  style={cnMono}
                  value={v.mcpToken ?? ''}
                  onChange={(e) => setV((p) => ({ ...p, mcpToken: e.target.value }))}
                />
                <div style={cnHint}>
                  Provide the MCP endpoint URL. Tools will be discovered automatically
                  on connect. A token is stored in AWS Secrets Manager and never shown
                  again; it is sent as <code>Authorization: Bearer &lt;token&gt;</code>{' '}
                  (a token that already starts with <code>Bearer </code> is sent as
                  entered). Leave it blank for a server without auth.
                </div>
              </>
            )}
            {v.connType === 'athena' && (
              <>
                {/* Amazon Athena is a typed connector: it carries non-secret
                    settings in a structured connection_config (no single
                    connection string). Fields map 1:1 to
                    ConnectRequest.connection_config. */}
                {athenaField('workgroup', 'Workgroup', 'primary', true)}
                {athenaField('database', 'Database / Schema', 'my_glue_database', true)}
                {athenaField('catalog', 'Catalog', 'AwsDataCatalog', false)}
                {athenaField('region', 'Region', 'us-east-1 (optional)', false)}
                {athenaField(
                  'output',
                  'Output Location',
                  's3://amzn-s3-demo-athena-results/ (optional)',
                  false,
                )}
                {athenaField(
                  'roleArn',
                  'Read-Only Role ARN',
                  'arn:aws:iam::111122223333:role/ADTCrossAccountDataReadOnly',
                  true,
                )}
                {athenaField('externalId', 'External ID', '', false)}
                <div style={cnHint}>
                  Schema is discovered via Athena <code>information_schema</code>{' '}
                  (metadata only). Catalog defaults to <code>AwsDataCatalog</code>;
                  region and output location are optional. A read-only IAM role is
                  required: this app&rsquo;s own roles cannot query Athena. Deploy{' '}
                  <code>stacks/cross-account/adt-data-readonly-role.yaml</code> in the
                  account that holds the data, with <code>TrustedAccountId</code> set to
                  this app&rsquo;s account (DataAccountId defaults to the account you
                  deploy the role in; set S3TablesAccountId only when your tables are in
                  Amazon S3 Tables, to the account that owns the S3 Tables catalog;
                  set DataBucketName for a plain Amazon S3 source; left empty, the role
                  gets no plain S3 access), make sure the app is deployed with{' '}
                  <code>--cross-account-role convention</code> (or that role&rsquo;s ARN),
                  then enter the role ARN and the External ID you deployed it with.
                </div>
              </>
            )}
            {v.connType === 'sitewise-direct' && (
              <>
                {/* AWS IoT SiteWise (Direct) uses IAM-role auth — no URL, no
                    token. Region + OPTIONAL cross-account Role ARN / External ID
                    flow into connection_config (role_arn/external_id) so a
                    SiteWise source in ANOTHER account can be discovered and
                    queried via an assumed role. */}
                <label style={cnLabel}>Region</label>
                <input
                  id="sitewise-region"
                  placeholder="us-east-1 (optional — defaults to the app region)"
                  style={cnMono}
                  value={v.sitewise.region}
                  onChange={(e) => patchSitewise('region', e.target.value)}
                />
                <label style={cnLabel}>Cross-Account Role ARN</label>
                <input
                  id="sitewise-role-arn"
                  placeholder="arn:aws:iam::111122223333:role/MyRole"
                  style={cnMono}
                  value={v.sitewise.roleArn}
                  onChange={(e) => patchSitewise('roleArn', e.target.value)}
                />
                <label style={cnLabel}>External ID</label>
                <input
                  id="sitewise-external-id"
                  placeholder=""
                  style={cnMono}
                  value={v.sitewise.externalId}
                  onChange={(e) => patchSitewise('externalId', e.target.value)}
                />
                <div style={cnHint}>
                  Queries AWS IoT SiteWise directly via an IAM role (no URL or token).
                  Asset models and properties are discovered from the SiteWise API.
                  Region, Cross-Account Role ARN and External ID are optional — leave
                  blank to use the app&rsquo;s own account credentials.
                </div>
              </>
            )}
            {v.connType === 'sitewise-i3x' && (
              <>
                {/* SiteWise (i3X): a REST i3X server fronting AWS IoT SiteWise.
                    The endpoint is the server's base URL; the bearer token is
                    routed by the server into Secrets Manager (adt/conn/<id>)
                    at registration — it is never stored on the catalog entry. */}
                <label style={cnLabel}>i3X Endpoint URL {req}</label>
                <input
                  id="i3x-endpoint"
                  placeholder="https://i3x.example.com"
                  style={cnMono}
                  value={v.i3x.endpoint}
                  onChange={(e) => patchI3x('endpoint', e.target.value)}
                />
                <label style={cnLabel}>Bearer Token</label>
                <input
                  id="i3x-token"
                  type="password"
                  placeholder="required when the server enforces auth"
                  style={cnMono}
                  value={v.i3x.token}
                  onChange={(e) => patchI3x('token', e.target.value)}
                />
                <div style={cnHint}>
                  Connects to an i3X 1.0 REST server (for example the
                  aws-samples i3X SiteWise server). Namespaces, object types,
                  and the asset hierarchy are discovered from the i3X API. The
                  token is stored securely in AWS Secrets Manager — leave it
                  blank only if the server does not require authentication.
                </div>
              </>
            )}
            {v.connType === 'tulip' && (
              <>
                {/* Tulip (Tables API) is a first-class
                    connection type the USER declares — no host-sniffing. Two
                    credential SOURCES: "API key and secret" (today's fields:
                    the server encodes them to an HTTP Basic token and routes it
                    into Secrets Manager adt/conn/<id>), or "Secrets Manager
                    secret" (name an existing secret by ARN; the server reads
                    the key from it and pins the URL/workspace the secret
                    names). The ARN choice appears only when this deployment
                    enables it for Tulip (secretArnSourceOffered). No
                    workshop/seat wording — a generic second credential source. */}
                {secretArnSourceOffered('Tulip (Tables API)') && (
                  <fieldset id="tulip-cred-source" style={{ border: 'none', padding: 0, margin: 0 }}>
                    <label style={cnLabel}>Credentials</label>
                    <label style={{ display: 'block', cursor: 'pointer' }}>
                      <input
                        type="radio"
                        name="tulip-cred-source"
                        value="inline"
                        checked={v.tulip.credentialMode === 'inline'}
                        onChange={() => patchTulip('credentialMode', 'inline')}
                      />{' '}
                      API key and secret
                    </label>
                    <label style={{ display: 'block', cursor: 'pointer' }}>
                      <input
                        type="radio"
                        name="tulip-cred-source"
                        value="secret_arn"
                        checked={v.tulip.credentialMode === 'secret_arn'}
                        onChange={() => patchTulip('credentialMode', 'secret_arn')}
                      />{' '}
                      Secrets Manager secret
                    </label>
                  </fieldset>
                )}
                {v.tulip.credentialMode === 'secret_arn' ? (
                  <>
                    <label style={cnLabel}>Secrets Manager secret ARN {req}</label>
                    <input
                      id="tulip-secret-arn"
                      placeholder="arn:aws:secretsmanager:…:secret:…"
                      style={cnMono}
                      value={v.tulip.secretArn}
                      onChange={(e) => patchTulip('secretArn', e.target.value)}
                    />
                    <label style={cnLabel}>Tulip Base URL</label>
                    <input
                      id="tulip-base-url"
                      placeholder="optional — the secret supplies it"
                      style={cnMono}
                      value={v.tulip.baseUrl}
                      onChange={(e) => patchTulip('baseUrl', e.target.value)}
                    />
                    <label style={cnLabel}>Workspace ID</label>
                    <input
                      id="tulip-workspace-id"
                      placeholder="optional — the secret supplies it"
                      style={cnMono}
                      value={v.tulip.workspaceId}
                      onChange={(e) => patchTulip('workspaceId', e.target.value)}
                    />
                    <div style={cnHint}>
                      Names an existing AWS Secrets Manager secret that holds the
                      Tulip credentials. The key is read from the secret and
                      never leaves the server. The secret also fixes the base URL
                      and workspace it is scoped to; leave those blank to use the
                      secret&rsquo;s values, or they must match it.
                    </div>
                  </>
                ) : (
                  <>
                    <label style={cnLabel}>Tulip Base URL {req}</label>
                    <input
                      id="tulip-base-url"
                      placeholder="https://your-instance.tulip.co"
                      style={cnMono}
                      value={v.tulip.baseUrl}
                      onChange={(e) => patchTulip('baseUrl', e.target.value)}
                    />
                    <label style={cnLabel}>Workspace ID</label>
                    <input
                      id="tulip-workspace-id"
                      placeholder="only for an instance-wide (global) API key"
                      style={cnMono}
                      value={v.tulip.workspaceId}
                      onChange={(e) => patchTulip('workspaceId', e.target.value)}
                    />
                    <label style={cnLabel}>API Key</label>
                    <input
                      id="tulip-api-key"
                      placeholder="Tulip Tables API key"
                      style={cnMono}
                      value={v.tulip.apiKey}
                      onChange={(e) => patchTulip('apiKey', e.target.value)}
                    />
                    <label style={cnLabel}>API Secret</label>
                    <input
                      id="tulip-api-secret"
                      type="password"
                      placeholder="Tulip Tables API secret"
                      style={cnMono}
                      value={v.tulip.apiSecret}
                      onChange={(e) => patchTulip('apiSecret', e.target.value)}
                    />
                    <div style={cnHint}>
                      Connects to a Tulip instance via the Tulip Tables REST API.
                      Tables and columns are discovered from the Tables API. The
                      API key and secret are combined into an HTTP Basic token and
                      stored securely in AWS Secrets Manager — you don&rsquo;t need
                      to encode them yourself. Leave them blank only if the
                      instance does not require authentication. Set Workspace ID
                      only for an instance-wide (global) API key; a
                      workspace-scoped key needs no workspace.
                    </div>
                  </>
                )}
              </>
            )}
          </div>
          {connectNewError ? (
            <p className="connect-new-error" role="alert">
              {connectNewError}
            </p>
          ) : null}
          <button
            onClick={() => void startCustomOnboard(v)}
            disabled={connectNewBusy}
            aria-busy={connectNewBusy}
            style={{
              padding: '0.6rem 1.5rem',
              background: 'var(--accent)',
              color: '#fff',
              border: 'none',
              borderRadius: 4,
              cursor: connectNewBusy ? 'progress' : 'pointer',
              opacity: connectNewBusy ? 0.7 : 1,
              fontWeight: 600,
            }}
          >
            {connectNewBusy ? 'Connecting…' : 'Discover Schema →'}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

/** The open onboarding overlays, unportaled. ObModals mounts this under
 * <body>; a static (server) render, which cannot portal, renders it directly. */
export function ObModalLayer({ workshopMode = 'unknown' }: { workshopMode?: WorkshopMode } = {}) {
  const modal = useStore(flowStore, (s) => s.modal);
  const connectNewOpen = useStore(catalogStore, (s) => s.connectNewOpen);
  return (
    <>
      {modal?.kind === 'cancel' && <CancelModal then={modal.then} />}
      {modal?.kind === 'resetAll' && <ResetAllModal />}
      {modal?.kind === 'disconnect' && (
        <DisconnectModal
          systemId={modal.systemId}
          systemName={modal.systemName}
          registration={modal.registration}
          retryFull={modal.retryFull}
          retry={modal.retry}
          workshopMode={workshopMode}
          userOrigin={modal.userOrigin}
        />
      )}
      {connectNewOpen && <ConnectNewModal />}
    </>
  );
}

/** ``workshopMode``: OnboardView's useWorkshopMode() reading ('unknown', the
 * default, offers no "also forget" choice). */
export function ObModals({ workshopMode = 'unknown' }: { workshopMode?: WorkshopMode } = {}) {
  if (typeof document === 'undefined') return null;
  // Portaled to <body>: the onboard view keeps the view-enter animation's
  // transform (fill-mode both), which makes it a stacking context, so an
  // overlay rendered inside it sat under the open chat sidebar however high
  // its z-index. The dashboard's DetailModal portals for the same reason.
  return createPortal(<ObModalLayer workshopMode={workshopMode} />, document.body);
}
