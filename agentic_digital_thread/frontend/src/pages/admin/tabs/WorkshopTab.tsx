// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Workshop admin tab (ADR-0091) — the attendee's plant clock: phase bar,
 * the three facts a facilitator reads (phase, next bearing alarm with a
 * local countdown, sensor backfill), the clock's provenance (and, after a
 * live-only move, where the live clock sits), the recommended "Move the
 * bearing alarm, keep the history" card (ADR-0095: only the live side
 * moves), the original "rewrite the plant story" placement card, and the
 * Design C "Reset to the packaged default" failsafe behind a disclosure.
 *
 * Server state is the SHARED workshopStoryQueryOptions (one key, 60 s poll,
 * D7). The countdown ticks locally from `seconds_to_alarm` minus the seconds
 * since `dataUpdatedAt` — subtraction only, re-synced on every refetch.
 * A transient refetch failure keeps the last good clock on screen with a
 * small "refresh failed" note; only a 403/404 or no data hides the tab. */
import type { StoryClock } from '../../../api/types/workshop';
import {
  fill,
  formatClockTime,
  formatCountdown,
  phaseLabel,
  remainingSeconds,
} from '../../../features/workshop/clock';
import { useNow, useWorkshopStory } from '../../../features/workshop/useWorkshopStory';
import { useI18n } from '../../../lib/i18n';
import '../../../styles/workshop.css';
import PhaseBar from './workshop/PhaseBar';
import PlaceClockControl from './workshop/PlaceClockControl';
import ResetClockControl from './workshop/ResetClockControl';
import { anchorSourceLabel, backfillKey } from './workshop/workshop-logic';

const TICK_MS = 1_000;

function AlarmCountdown({ clock, fetchedAt }: { clock: StoryClock; fetchedAt: number }) {
  const { t } = useI18n();
  const now = useNow(TICK_MS);
  const timeline = clock.phase_timeline;
  const left = remainingSeconds(timeline.seconds_to_alarm, fetchedAt, now);
  return (
    <div className="workshop-stat workshop-field">
      <span className="workshop-stat-label workshop-field-label">{t('workshop.nextAlarm')}</span>
      <span className="workshop-stat-value">
        <time dateTime={timeline.next_alarm_at} data-testid="workshop-next-alarm">
          {formatClockTime(timeline.next_alarm_at)}
        </time>
        <span className="mono" data-testid="workshop-alarm-countdown">
          {fill(t('workshop.countdown'), { time: formatCountdown(left) })}
        </span>
      </span>
      {timeline.alarm_active && (
        <div className="workshop-alarm-active" data-testid="workshop-alarm-active">
          {t('workshop.alarmActive')}
        </div>
      )}
    </div>
  );
}

export default function WorkshopTab() {
  const { t } = useI18n();
  const { query, clock, refreshFailed } = useWorkshopStory();
  if (!clock) return null;
  const backfill = clock.telemetry?.backfill_complete;
  return (
    <div className="workshop-tab" data-testid="workshop-tab">
      <div className="workshop-head">
        <h3 className="workshop-title">{t('workshop.title')}</h3>
        <p className="workshop-help">{t('workshop.help')}</p>
      </div>
      {refreshFailed && (
        <div role="status" className="workshop-refresh-failed" data-testid="workshop-refresh-failed">
          {t('workshop.refreshFailed')}
        </div>
      )}

      <section className="workshop-hero" aria-label={t('workshop.title')}>
        <PhaseBar timeline={clock.phase_timeline} />

        <div className="workshop-stats workshop-fields">
          <div className="workshop-stat workshop-field">
            <span className="workshop-stat-label workshop-field-label">{t('workshop.currentPhase')}</span>
            <span className="workshop-stat-value" data-testid="workshop-current-phase">
              {phaseLabel(clock.phase_timeline.current, t)}
            </span>
          </div>
          <AlarmCountdown clock={clock} fetchedAt={query.dataUpdatedAt} />
          <div className="workshop-stat workshop-field">
            <span className="workshop-stat-label workshop-field-label">{t('workshop.backfillLabel')}</span>
            <span
              className="workshop-stat-value"
              role="status"
              data-testid="workshop-backfill"
              data-complete={backfill === true ? 'true' : backfill === false ? 'false' : 'unknown'}
            >
              {backfill === false && <span className="workshop-spinner" aria-hidden="true" />}
              {t(backfillKey(backfill))}
            </span>
          </div>
        </div>

        <p className="workshop-provenance workshop-field">
          <span className="workshop-field-label">{t('workshop.anchorSourceLabel')}</span>{' '}
          <span data-testid="workshop-anchor-source" data-source={clock.anchor_source ?? ''}>
            {anchorSourceLabel(clock.anchor_source, t)}
          </span>
          {clock.live_anchor_source === 'placed-live' && clock.live_anchor_at && (
            <span className="workshop-live-clock" data-testid="workshop-live-clock">
              {' · '}
              {fill(t('workshop.live.provenance'), { time: formatClockTime(clock.live_anchor_at) })}
            </span>
          )}
        </p>
      </section>

      <section className="workshop-card workshop-card-recommended" aria-labelledby="workshop-live-heading"
        data-testid="workshop-live-card">
        <h4 id="workshop-live-heading" className="workshop-card-title">
          {t('workshop.live.heading')}
          <span className="workshop-card-badge">{t('workshop.live.recommended')}</span>
        </h4>
        <p className="workshop-card-help">{t('workshop.live.help')}</p>
        <PlaceClockControl variant="live" anchorEpoch={clock.anchor_epoch}
          liveAnchorEpoch={clock.live_anchor_epoch} timeline={clock.phase_timeline} />
      </section>

      <section className="workshop-card" aria-labelledby="workshop-place-heading">
        <h4 id="workshop-place-heading" className="workshop-card-title">{t('workshop.placeHeading')}</h4>
        <p className="workshop-card-help">{t('workshop.placeHelp')}</p>
        <PlaceClockControl anchorEpoch={clock.anchor_epoch} timeline={clock.phase_timeline} />
      </section>

      <ResetClockControl
        anchorEpoch={clock.anchor_epoch}
        defaultAnchorEpoch={clock.default_anchor_epoch}
        defaultAnchorIso={clock.default_anchor_iso}
      />
    </div>
  );
}
