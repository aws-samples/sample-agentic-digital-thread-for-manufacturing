// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <PlaceClockControl/> — "Bearing alarm in [N] minutes" → Preview → Place.
 *
 * Preview POSTs `{alarm_in_minutes: N, expected_anchor_epoch: A, dry_run: true}`
 * (D1: `dry_run` is the one wire name) and the server resolves N against ONE
 * `now`. Place stays disabled until a preview succeeded for the SAME N and
 * the SAME anchor, less than PREVIEW_TTL_MS ago; a disabled Place is
 * `aria-describedby` the text saying why. It opens a confirm dialog that
 * shows the previewed alarm time and effect, and only Confirm sends the
 * ABSOLUTE `{alarm_at: preview.alarm_at_iso, expected_anchor_epoch: A,
 * dry_run: false}` — the applied alarm is exactly the one previewed, never
 * re-resolved against a later `now`. N's upper bound is one loop, from the
 * server's phase_timeline. Both buttons are disabled while any request is
 * pending. Every server string renders as a literal text node (zero HTML sinks).
 *
 * Two variants (ADR-0095). `variant="full"` (default) is the original
 * placement: it rewrites the plant history and its CAS anchor is the history
 * anchor. `variant="live"` adds `keep_history: true` to both requests, its CAS
 * anchor is the LIVE anchor (`live_anchor_epoch`, falling back to the history
 * anchor on an older server), its test ids are prefixed `workshop-live-` and
 * its copy comes from the `workshop.live.*` keys. */
import { useId, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import type {
  PhaseTimeline,
  PlaceClockApplyBody,
  PlaceClockPreview,
  PlaceClockPreviewBody,
} from '../../../../api/types/workshop';
import { fill, formatClockTime } from '../../../../features/workshop/clock';
import { placeClockMutationOptions } from '../../../../features/workshop/queries';
import { useNow } from '../../../../features/workshop/useWorkshopStory';
import { useI18n } from '../../../../lib/i18n';
import { ConfirmDialog } from '../../../../shared/ui/ConfirmDialog';
import PreviewPanel, { LineList } from './PreviewPanel';
import {
  DEFAULT_ALARM_MINUTES,
  effectLines,
  errorMessage,
  maxAlarmMinutes,
  MIN_ALARM_MINUTES,
  placeBlockedKey,
  previewExpired,
  previewMatches,
  previewSummary,
  validateMinutes,
  warningLines,
  type PreviewRecord,
} from './workshop-logic';

/** How often an open preview re-checks its PREVIEW_TTL_MS expiry. */
export const PREVIEW_EXPIRY_CHECK_MS = 5_000;

export type PlaceVariant = 'full' | 'live';

interface PlaceClockProps {
  anchorEpoch: number;
  timeline: PhaseTimeline;
  variant?: PlaceVariant;
  /** The live anchor (ADR-0095); the `live` variant's CAS anchor. */
  liveAnchorEpoch?: number;
}

/** Per-variant test-id prefix, copy keys and confirm tone. */
const VARIANTS = {
  full: {
    id: 'workshop',
    keys: { place: 'workshop.place', placing: 'workshop.placing', placed: 'workshop.placed',
      confirmTitle: 'workshop.confirmTitle', confirmBody: 'workshop.confirmBody', confirmPlace: 'workshop.confirmPlace' },
    tone: 'danger' as const,
  },
  live: {
    id: 'workshop-live',
    keys: { place: 'workshop.live.place', placing: 'workshop.live.placing', placed: 'workshop.live.placed',
      confirmTitle: 'workshop.live.confirmTitle', confirmBody: 'workshop.live.confirmBody',
      confirmPlace: 'workshop.live.confirmPlace' },
    tone: 'default' as const,
  },
};

/** State + handlers for the control; the render stays declarative. */
function usePlaceClock({ anchorEpoch, timeline, variant = 'full', liveAnchorEpoch }: PlaceClockProps) {
  const client = useQueryClient();
  const mutation = useMutation(placeClockMutationOptions(client));
  const [minutesText, setMinutesText] = useState(String(DEFAULT_ALARM_MINUTES));
  const [previewRecord, setPreviewRecord] = useState<PreviewRecord | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [placed, setPlaced] = useState(false);
  const now = useNow(previewRecord ? PREVIEW_EXPIRY_CHECK_MS : null, previewRecord);

  const live = variant === 'live';
  const casAnchor = live ? liveAnchorEpoch ?? anchorEpoch : anchorEpoch;
  const keep = live ? { keep_history: true as const } : {};
  const maxMinutes = maxAlarmMinutes(timeline);
  const minutes = validateMinutes(minutesText, maxMinutes);
  const pending = mutation.isPending;
  const matches = previewMatches(previewRecord, minutes, casAnchor);
  const expired = matches && previewExpired(previewRecord, now);
  const blockedKey = placeBlockedKey(matches, expired);

  const runPreview = () => {
    if (!minutes.ok || pending) return;
    const request: PlaceClockPreviewBody = { alarm_in_minutes: minutes.minutes, expected_anchor_epoch: casAnchor, dry_run: true, ...keep };
    setPlaced(false);
    mutation.mutate(request, {
      onSuccess: (result) => setPreviewRecord({
        minutes: request.alarm_in_minutes,
        anchorEpoch: request.expected_anchor_epoch,
        previewedAt: Date.now(),
        preview: result as PlaceClockPreview,
      }),
    });
  };
  const runPlace = () => {
    setConfirmOpen(false);
    if (pending || blockedKey || !previewRecord) return;
    const { preview, anchorEpoch: expected } = previewRecord;
    const request: PlaceClockApplyBody = { alarm_at: preview.alarm_at_iso, expected_anchor_epoch: expected, dry_run: false, ...keep };
    mutation.mutate(request, { onSuccess: () => { setPlaced(true); setPreviewRecord(null); } });
  };

  const preview = matches ? previewRecord?.preview ?? null : null;
  return { mutation, minutesText, setMinutesText, minutes, maxMinutes, pending, expired, blockedKey, preview, placed,
    confirmOpen, setConfirmOpen, runPreview, runPlace, variant: VARIANTS[variant] };
}

type PlaceClock = ReturnType<typeof usePlaceClock>;

/** Minutes input + Preview + Place; a disabled Place is described by `reasonId`. */
function PlaceRow({ state, inputId, hintId, reasonId }: { state: PlaceClock; inputId: string; hintId: string; reasonId: string }) {
  const { t } = useI18n();
  const { mutation, minutes, pending, blockedKey, variant } = state;
  const pendingDryRun = pending && mutation.variables?.dry_run === true;
  const pendingPlace = pending && mutation.variables?.dry_run === false;
  return (
    <div className="workshop-place-row">
      <label htmlFor={inputId}>{t('workshop.alarmInPrefix')}</label>
      <input id={inputId} type="number" inputMode="numeric" min={MIN_ALARM_MINUTES} max={state.maxMinutes} step={1}
        value={state.minutesText} aria-describedby={hintId} aria-invalid={minutes.ok ? undefined : true}
        onChange={(e) => state.setMinutesText(e.target.value)} data-testid={`${variant.id}-minutes-input`} />
      <span>{t('workshop.alarmInSuffix')}</span>
      <button type="button" onClick={state.runPreview} disabled={pending || !minutes.ok} data-testid={`${variant.id}-preview-btn`}>
        {pendingDryRun ? t('workshop.previewing') : t('workshop.preview')}
      </button>
      <button type="button" className="workshop-place-btn" onClick={() => state.setConfirmOpen(true)}
        disabled={pending || !!blockedKey} title={blockedKey ? t(blockedKey) : undefined}
        aria-describedby={blockedKey ? reasonId : undefined} data-testid={`${variant.id}-place-btn`}>
        {pendingPlace ? t(variant.keys.placing) : t(variant.keys.place)}
      </button>
    </div>
  );
}

/** The ConfirmDialog body: the exact instant Confirm sends as `alarm_at`. */
export function PlaceConfirmBody({ preview, lines, bodyKey = 'workshop.confirmBody', testIdPrefix = 'workshop' }:
  { preview: PlaceClockPreview; lines: string[]; bodyKey?: string; testIdPrefix?: string }) {
  const { t } = useI18n();
  return (
    <div data-testid={`${testIdPrefix}-confirm-body`}>
      <p style={{ margin: '0 0 0.4rem' }} data-testid={`${testIdPrefix}-confirm-alarm`} data-alarm-at={preview.alarm_at_iso}>
        {fill(t(bodyKey), { time: formatClockTime(preview.alarm_at_iso) })}
      </p>
      <LineList lines={lines} testId={`${testIdPrefix}-confirm-effect`} style={{ margin: 0 }} />
    </div>
  );
}

export default function PlaceClockControl(props: PlaceClockProps) {
  const { t } = useI18n();
  const inputId = useId();
  const hintId = useId();
  const reasonId = useId();
  const state = usePlaceClock(props);
  const { mutation, minutes, preview, blockedKey, variant } = state;
  const effect = preview ? effectLines(preview, t) : [];
  const warnings = preview ? warningLines(preview, t) : [];
  const range = { min: MIN_ALARM_MINUTES, max: state.maxMinutes };
  const id = variant.id;

  return (
    <div className="workshop-place" data-testid={`${id}-place-control`}>
      <PlaceRow state={state} inputId={inputId} hintId={hintId} reasonId={reasonId} />
      <div id={hintId} className="workshop-hint" data-testid={`${id}-minutes-hint`}>
        {fill(t(minutes.ok ? 'workshop.minutesHint' : 'workshop.minutesInvalid'), range)}
      </div>
      {blockedKey && <span id={reasonId} className="visually-hidden" data-testid={`${id}-place-reason`}>{t(blockedKey)}</span>}
      {preview && (
        <PreviewPanel testIdPrefix={`${id}-preview`} expired={state.expired} summary={previewSummary(preview, t)}
          effectHeading={t('workshop.effect')} effect={effect} warnings={warnings} />
      )}
      {mutation.isError && (
        <div role="alert" className="workshop-error" data-testid={`${id}-place-error`}>{errorMessage(mutation.error, t)}</div>
      )}
      {state.placed && !state.pending && (
        <div role="status" className="workshop-placed" data-testid={`${id}-placed`}>{t(variant.keys.placed)}</div>
      )}
      <ConfirmDialog open={state.confirmOpen && !blockedKey && !!preview} title={t(variant.keys.confirmTitle)}
        confirmLabel={t(variant.keys.confirmPlace)} tone={variant.tone}
        onCancel={() => state.setConfirmOpen(false)} onConfirm={state.runPlace}>
        {preview && <PlaceConfirmBody preview={preview} lines={[...effect, ...warnings]} bodyKey={variant.keys.confirmBody} testIdPrefix={id} />}
      </ConfirmDialog>
    </div>
  );
}
