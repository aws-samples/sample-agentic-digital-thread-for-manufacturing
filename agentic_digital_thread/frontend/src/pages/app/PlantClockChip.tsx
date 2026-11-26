// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* PlantClockChip — the attendee-visible, READ-ONLY plant clock in the app
 * header (ADR-0091): "Plant clock: degrading, alarm in 18 min".
 *
 * Server state is the SHARED workshop story query (ADR-0091 D7: one key, one
 * 60 s poll — the header chip and the admin Workshop tab share a single
 * request). The GET is steward-gated, so the query is enabled only for
 * admins; a 403/404, loading with no data, and the frozen story
 * (`clock: null`) render NOTHING; a transient refetch failure keeps the last
 * good clock (features/workshop/clock.clockGate).
 *
 * Provenance (Design C, `data-state` + `data-source` = anchor_source, and the
 * source in the title): a placed clock reads "Plant clock: …" (a stored
 * live-only move counts as placed, and the title then names `placed`); the
 * packaged/deploy default reads "Plant clock (default): …"; while this page's
 * first-sign-in auto-place is in flight "Plant clock: starting…"; when it
 * failed "Plant clock: not started. Ask for help." in a role=alert region
 * (mounted once, static text — announced once).
 *
 * The countdown ticks locally every 30 s from the server's seconds minus the
 * time since `dataUpdatedAt` (subtraction only), re-synced on every refetch.
 * The ticking text is `aria-live="off"`; the polite status region announces
 * only a phase change (a visually-hidden phase line). The chip is wrapped in
 * a plain link to the admin Workshop tab (`/admin.html#tab-workshop`), where
 * the clock is explained and can be moved (ADR-0095); the link carries the
 * title and an aria-label, the status region inside it is unchanged. Plain
 * React text nodes — zero innerHTML. */
import type { StoryClock } from '../../api/types/workshop';
import { autoPlaceStore } from '../../features/workshop/auto-place';
import { effectiveAnchorSource, remainingSeconds } from '../../features/workshop/clock';
import { useNow, useWorkshopStory } from '../../features/workshop/useWorkshopStory';
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import '../../styles/plant-clock-chip.css';
import {
  chipAnnouncement,
  chipServerSeconds,
  chipSourceTitle,
  chipState,
  chipText,
  type ChipState,
} from './plant-clock-logic';

export const CHIP_TICK_MS = 30_000;
/** Where the chip leads: the admin page, Workshop tab (AdminPage restores the tab from the hash). */
export const CHIP_HREF = '/admin.html#tab-workshop';

function ChipBody({ clock, state, fetchedAt }: { clock: StoryClock; state: ChipState; fetchedAt: number }) {
  const { t } = useI18n();
  const timeline = clock.phase_timeline;
  const now = useNow(CHIP_TICK_MS, fetchedAt);
  const left = remainingSeconds(chipServerSeconds(timeline), fetchedAt, now);
  const failed = state === 'failed';
  const text = failed
    ? t('plantClock.chip.failed')
    : state === 'starting'
      ? t('plantClock.chip.starting')
      : chipText(timeline.current, left, t, state === 'default');
  const manage = t('plantClock.chip.manage');
  return (
    <a
      href={CHIP_HREF}
      className="plant-clock-chip-link"
      aria-label={`${text}. ${manage}`}
      data-testid="plant-clock-chip-link"
      data-live={clock.live_anchor_source === 'placed-live' ? 'true' : 'false'}
    >
      <span
        // A fresh node for the failed state: the alert region mounts once.
        key={failed ? 'failed' : 'clock'}
        className={`plant-clock-chip plant-clock-chip-${failed ? 'failed' : timeline.current}`}
        role={failed ? 'alert' : 'status'}
        aria-live={failed ? undefined : 'polite'}
        data-testid="plant-clock-chip"
        data-phase={timeline.current}
        data-state={state}
        data-source={clock.anchor_source ?? ''}
        title={`${chipSourceTitle(timeline, effectiveAnchorSource(clock), t)} · ${manage}`}
      >
        <span className="plant-clock-chip-dot" aria-hidden="true" />
        <span aria-live={failed ? undefined : 'off'} data-testid="plant-clock-chip-text">
          {text}
        </span>
        {!failed && (
          <span className="visually-hidden" data-testid="plant-clock-chip-announce">
            {chipAnnouncement(timeline.current, t)}
          </span>
        )}
      </span>
    </a>
  );
}

export default function PlantClockChip() {
  const { query, clock } = useWorkshopStory();
  const autoPlace = useStore(autoPlaceStore);
  if (!clock) return null;
  return <ChipBody clock={clock} state={chipState(clock, autoPlace)} fetchedAt={query.dataUpdatedAt} />;
}
