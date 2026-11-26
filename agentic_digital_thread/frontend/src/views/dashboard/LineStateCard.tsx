// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Line State panel (S5.4) — React half of the old lineState.ts renderLineState
 * (jsx-migration W2.1). Renders the #line-state-card skeleton (ids/classes are
 * CONTRACTUAL: #ls-running #ls-idle #ls-down #ls-sites #ls-systems-badge
 * #line-state-diag, .line-state-site-group / .line-state-line-chip,
 * .diag-badge) from computeLineState over the store's line-state payload.
 * The [data-requires="MES,CMMS"] show/hide (old loadSystems querySelectorAll
 * loop) is derived from the store's connected systems. */
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import { EmptyState, ErrorState, LoadingState } from '../../shared/ui/AsyncStates';
import { computeLineState, type LineStateBuckets } from './lib';
import { loadLineState, openLineStateDrill } from './lineState';
import { dashboardStore } from './store';

const REQUIRES = ['MES', 'CMMS'];

export function LineStateCard() {
  const { t } = useI18n();
  const payload = useStore(dashboardStore, (s) => s.lineState);
  const systems = useStore(dashboardStore, (s) => s.systems);

  const model = payload === undefined ? null : computeLineState(payload);
  const badge = model ? model.badge : 'machine-state';
  const diag = model ? model.diag : '';
  const asyncBody =
    payload === undefined ? (
      <LoadingState label="Loading line state" compact />
    ) : model?.kind === 'uncovered' ? (
      <EmptyState reason="not-configured" message={model.diag} compact />
    ) : model?.kind === 'error' ? (
      <ErrorState
        message={payload.error || 'Line state unavailable.'}
        onRetry={() => void loadLineState()}
        compact
      />
    ) : model?.kind === 'empty' ? (
      <EmptyState message="No line-state rows were returned." compact />
    ) : null;

  // Counts + dim styling: active states highlighted, zero/unknown dimmed.
  let counts: { running: string; idle: string; down: string };
  let buckets: LineStateBuckets | null;
  if (!model) {
    counts = { running: '\u2014', idle: '\u2014', down: '\u2014' };
    buckets = null; // initial: no dim classes (untouched skeleton)
  } else if (model.kind === 'error' || model.kind === 'uncovered') {
    counts = { running: '\u2014', idle: '\u2014', down: '\u2014' };
    buckets = { running: 0, idle: 0, down: 0 }; // setDim(null): all dimmed
  } else {
    counts = {
      running: String(model.headline.running),
      idle: String(model.headline.idle),
      down: String(model.headline.down),
    };
    buckets = model.headline;
  }
  const dim = (k: keyof LineStateBuckets) => (model && (!buckets || !buckets[k]) ? ' dim' : '');

  // data-requires visibility (loadSystems): shown while systems are unknown,
  // then only when a connected system of a required type exists.
  const connectedTypes = new Set(
    (systems || []).filter((s) => s.status === 'connected').map((s) => s.type),
  );
  const visible = systems === null || REQUIRES.some((t) => connectedTypes.has(t));

  return (
    <div
      className="line-state-card diag-reveal drillable"
      id="line-state-card"
      data-requires="MES,CMMS"
      style={{ display: visible ? '' : 'none' }}
      onClick={() => void openLineStateDrill()}
    >
      <div className="panel-title">
        {t('dash.lineState')}{' '}
        <span className="src-badge" id="ls-systems-badge">
          {badge}
        </span>
        <span className="diag-badge">diag</span>
      </div>
      <div className="line-state-counts">
        <div className="line-state-count">
          <div className={'line-state-num good' + dim('running')} id="ls-running">
            {counts.running}
          </div>
          <div className="line-state-label">{t('dash.running')}</div>
        </div>
        <div className="line-state-count">
          <div
            className={'line-state-num' + dim('idle')}
            style={{ color: 'var(--muted)' }}
            id="ls-idle"
          >
            {counts.idle}
          </div>
          <div className="line-state-label">{t('dash.idle')}</div>
        </div>
        <div className="line-state-count">
          <div className={'line-state-num highlight' + dim('down')} id="ls-down">
            {counts.down}
          </div>
          <div className="line-state-label">{t('dash.down')}</div>
        </div>
      </div>
      <div className="line-state-sites" id="ls-sites">
        {asyncBody}
        {model && model.kind === 'data'
          ? model.groups.map((group, gi) => (
              // Per-site groups: a SITE label + color-coded line chips.
              <div key={gi} className="line-state-site-group">
                <div className="line-state-site-label">{group.label}</div>
                <div className="line-state-site-chips">
                  {group.chips.map((c, ci) => (
                    <span key={ci} className={'line-state-line-chip line-chip-' + c.bucket}>
                      {c.text}
                    </span>
                  ))}
                </div>
              </div>
            ))
          : null}
      </div>
      <div className="line-state-caption">
        {t('dash.lineStateCaption')} <b>{t('dash.lineStateClick')}</b>
      </div>
      <div className="diag-note" id="line-state-diag">
        {diag}
      </div>
    </div>
  );
}
