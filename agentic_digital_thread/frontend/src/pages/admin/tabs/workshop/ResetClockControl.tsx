// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ResetClockControl/> — the Design C failsafe "Reset to the packaged
 * default" → Preview → Reset (behind the ConfirmDialog).
 *
 * Preview POSTs `{reset: true, expected_anchor_epoch: A, dry_run: true}` and
 * shows the default alarm/anchor plus the effect lines (`effect_codes`
 * localized — `restores_default` …). Reset stays disabled until a preview
 * succeeded for the SAME anchor, less than PREVIEW_TTL_MS ago; Confirm sends
 * `{reset: true, expected_anchor_epoch: A, dry_run: false}`. The control is
 * disabled with a hint when the GET carries no `default_anchor_epoch` (null,
 * or absent on an older server); a disabled Reset is `aria-describedby` the
 * text saying why. A 409 (in flight / stale anchor) is shown and re-reads
 * the story, exactly like Place (the shared mutation options).
 *
 * A successful reset sets the one-shot `adt.autoPlace.suppressed` session
 * flag (features/workshop/auto-place) to the anchor it placed, from the
 * MUTATION-level onSuccess — TanStack drops a mutate-level callback once
 * this control unmounts, so leaving the tab mid-reset would lose it. Going
 * back to the app then does not immediately auto-place over the reset; a
 * reload after that auto-places again unless the packaged `auto_place` flag
 * is off. Literal text nodes only — zero HTML sinks. */
import { useId, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import type { ResetClockBody, ResetClockPreview } from '../../../../api/types/workshop';
import { suppressAutoPlace } from '../../../../features/workshop/auto-place';
import { fill, formatClockTime } from '../../../../features/workshop/clock';
import { placeClockMutationOptions, type PlaceClockResult } from '../../../../features/workshop/queries';
import { useNow } from '../../../../features/workshop/useWorkshopStory';
import { useI18n } from '../../../../lib/i18n';
import { ConfirmDialog } from '../../../../shared/ui/ConfirmDialog';
import { PREVIEW_EXPIRY_CHECK_MS } from './PlaceClockControl';
import PreviewPanel, { LineList } from './PreviewPanel';
import {
  effectLines,
  errorMessage,
  previewExpired,
  resetAppliedAnchor,
  resetAvailable,
  resetBlockedKey,
  resetPreviewSummary,
  warningLines,
  type ResetPreviewRecord,
} from './workshop-logic';

interface ResetClockProps {
  anchorEpoch: number;
  defaultAnchorEpoch: number | null | undefined;
  defaultAnchorIso: string | null | undefined;
}

/** The reset mutation, keyed so a successful APPLY always sets the
 * suppression flag — even after this control unmounted. */
function useResetMutation(record: ResetPreviewRecord | null, defaultAnchorEpoch: number | null | undefined) {
  const client = useQueryClient();
  return useMutation({
    ...placeClockMutationOptions(client),
    onSuccess: (result: PlaceClockResult, body) => {
      if (!('reset' in body) || body.dry_run || typeof defaultAnchorEpoch !== 'number') return;
      suppressAutoPlace(resetAppliedAnchor(result, record?.preview, defaultAnchorEpoch));
    },
  });
}

/** State + handlers for the control; the render stays declarative. */
function useResetClock({ anchorEpoch, defaultAnchorEpoch }: ResetClockProps) {
  const [record, setRecord] = useState<ResetPreviewRecord | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [done, setDone] = useState(false);
  const mutation = useResetMutation(record, defaultAnchorEpoch);
  const now = useNow(record ? PREVIEW_EXPIRY_CHECK_MS : null, record);

  const available = resetAvailable(defaultAnchorEpoch);
  const pending = mutation.isPending;
  const matches = !!record && record.anchorEpoch === anchorEpoch;
  const expired = matches && previewExpired(record, now);
  const blockedKey = resetBlockedKey(available, matches, expired);

  const send = (dryRun: boolean, onSuccess: (result: ResetClockPreview) => void) => {
    const body: ResetClockBody = { reset: true, expected_anchor_epoch: anchorEpoch, dry_run: dryRun };
    mutation.mutate(body, { onSuccess: (result) => onSuccess(result as ResetClockPreview) });
  };
  const runPreview = () => {
    if (!available || pending) return;
    setDone(false);
    const previewedFor = anchorEpoch;
    send(true, (result) => setRecord({ anchorEpoch: previewedFor, previewedAt: Date.now(), preview: result }));
  };
  const runReset = () => {
    setConfirmOpen(false);
    if (pending || blockedKey) return;
    send(false, () => {
      setDone(true);
      setRecord(null);
    });
  };

  const preview = matches ? record?.preview ?? null : null;
  return { mutation, available, pending, expired, blockedKey, preview, done, confirmOpen, setConfirmOpen, runPreview, runReset };
}

type ResetClock = ReturnType<typeof useResetClock>;

/** The dry-run result: the default alarm/anchor, effect and warnings. */
export function ResetPreviewPanel({ expired, summary, effect, warnings }: {
  expired: boolean;
  summary: string[];
  effect: string[];
  warnings: string[];
}) {
  const { t } = useI18n();
  return (
    <PreviewPanel
      testIdPrefix="workshop-reset-preview"
      expired={expired}
      summary={summary}
      effectHeading={t('workshop.resetEffect')}
      effect={effect}
      warnings={warnings}
    />
  );
}

/** The ConfirmDialog body: every previewed line, as Confirm will apply it. */
export function ResetConfirmBody({ lines }: { lines: string[] }) {
  const { t } = useI18n();
  return (
    <div data-testid="workshop-reset-confirm-body">
      <p style={{ margin: '0 0 0.4rem' }}>{t('workshop.resetConfirmBody')}</p>
      <LineList lines={lines} style={{ margin: 0 }} />
    </div>
  );
}

/** Preview + Reset buttons; a disabled Reset is described by the reason
 * (the visible hint when there is no default, else `reasonId`). */
function ResetButtons({ state, hintId, reasonId }: { state: ResetClock; hintId: string; reasonId: string }) {
  const { t } = useI18n();
  const { mutation, pending, available, blockedKey } = state;
  const pendingDryRun = pending && mutation.variables?.dry_run === true;
  const pendingReset = pending && mutation.variables?.dry_run === false;
  return (
    <div className="workshop-place-row">
      <button type="button" onClick={state.runPreview} disabled={pending || !available}
        aria-describedby={hintId} data-testid="workshop-reset-preview-btn">
        {pendingDryRun ? t('workshop.previewing') : t('workshop.resetPreview')}
      </button>
      <button type="button" className="workshop-place-btn" onClick={() => state.setConfirmOpen(true)}
        disabled={pending || !!blockedKey}
        title={blockedKey && available ? t(blockedKey) : undefined}
        aria-describedby={blockedKey ? (available ? reasonId : hintId) : undefined}
        data-testid="workshop-reset-btn">
        {pendingReset ? t('workshop.resetting') : t('workshop.reset')}
      </button>
    </div>
  );
}

export default function ResetClockControl(props: ResetClockProps) {
  const { t } = useI18n();
  const hintId = useId();
  const reasonId = useId();
  const state = useResetClock(props);
  const { mutation, preview, expired, blockedKey, available } = state;
  const summary = preview ? resetPreviewSummary(preview, props.defaultAnchorIso, t) : [];
  const effect = preview ? effectLines(preview, t) : [];
  const warnings = preview ? warningLines(preview, t) : [];

  return (
    <details className="workshop-advanced" data-testid="workshop-reset-control">
      <summary className="workshop-reset-title">{t('workshop.resetHeading')}</summary>
      <div className="workshop-place workshop-reset">
        <div id={hintId} className="workshop-hint" data-testid="workshop-reset-hint">
          {available
            ? fill(t('workshop.resetHelp'), { time: formatClockTime(props.defaultAnchorIso) })
            : t('workshop.resetUnavailable')}
        </div>
        <ResetButtons state={state} hintId={hintId} reasonId={reasonId} />
        {blockedKey && available && (
          <span id={reasonId} className="visually-hidden" data-testid="workshop-reset-reason">{t(blockedKey)}</span>
        )}
        {preview && <ResetPreviewPanel expired={expired} summary={summary} effect={effect} warnings={warnings} />}
        {mutation.isError && (
          <div role="alert" className="workshop-error" data-testid="workshop-reset-error">
            {errorMessage(mutation.error, t)}
          </div>
        )}
        {state.done && !state.pending && (
          <div role="status" className="workshop-placed" data-testid="workshop-reset-done">{t('workshop.resetDone')}</div>
        )}
      </div>
      <ConfirmDialog open={state.confirmOpen && !blockedKey && !!preview} title={t('workshop.resetConfirmTitle')}
        confirmLabel={t('workshop.resetConfirm')} tone="danger"
        onCancel={() => state.setConfirmOpen(false)} onConfirm={state.runReset}>
        {preview && <ResetConfirmBody lines={[...summary, ...effect, ...warnings]} />}
      </ConfirmDialog>
    </details>
  );
}
