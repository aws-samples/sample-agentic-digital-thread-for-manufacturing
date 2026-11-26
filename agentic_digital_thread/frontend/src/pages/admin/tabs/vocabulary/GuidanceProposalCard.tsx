// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import { t, tf } from '../../../../lib/i18n';
import { push as pushToast } from '../../../../shared/ui/toast-store';
import type {
  GuidanceAcceptResult,
  GuidanceEvictionConfirmation,
  GuidancePreview,
  ProposedMapping,
} from '../../../../api/types/proposals';
import {
  acceptGuidanceMutationOptions,
  guidancePreviewQueryOptions,
} from '../../guidance-queries';
import { GuidanceLossDialog } from './GuidanceLossDialog';
import {
  actionStyle,
  AuthorityTag,
  compactStyle,
  isStrictGuidanceDelivery,
  MONO,
  numberOrUnavailable,
  panelStyle,
  stateColor,
  systemDisplayLabel,
  useSystemDisplayNames,
  type AuthorityState,
} from './guidance-stability-ui';

interface Feedback {
  state: AuthorityState;
  /** One plain sentence for the attendee. */
  line: string;
  /** Steward detail shown only under "Technical detail". */
  detail?: string;
}

// Plain badge words on the card. `data-state` (tests, e2e) keeps the state id.
const CARD_LABEL: Partial<Record<AuthorityState, string>> = {
  PENDING: 'Not delivered',
  PREVIEW: 'Ready',
  DELIVERED: 'Delivered',
  'WOULD EVICT': 'Confirm removal',
  UNAVAILABLE: 'Unavailable',
};

const INITIAL_FEEDBACK: Feedback = { state: 'PENDING', line: 'Choose Preview delivery to see what changes.' };
const PREVIEW_UNREADABLE = 'The preview could not be read, so nothing changed. Choose Preview delivery to try again.';
const NOT_ACCEPTED = 'Delivery could not be confirmed, so nothing was accepted.';
const PROPOSAL_NOT_RESTORED =
  'Delivery could not be completed, and this note\'s status could not be set back to pending: it may show as accepted without being delivered. Reload the review queue before trying again.';

function previewFeedback(preview: GuidancePreview, isAdmin: boolean): Feedback {
  if (preview.outcome === 'would_evict') {
    return {
      state: 'WOULD EVICT',
      line: isAdmin
        ? 'This note fits only if older learned notes are removed. Choose Review what is removed.'
        : 'This note fits only if older learned notes are removed. A steward must confirm that first.',
    };
  }
  if (preview.outcome === 'unavailable') {
    return { state: 'UNAVAILABLE', line: PREVIEW_UNREADABLE };
  }
  if (preview.outcome === 'duplicate') {
    return { state: 'PENDING', line: 'This note is already in the guidance. Nothing to deliver.' };
  }
  if (preview.outcome === 'no_room') {
    return {
      state: 'PENDING',
      line: "This note does not fit in the system's guidance. Shorten it or remove an older learned note, then propose it again.",
    };
  }
  return { state: 'PREVIEW', line: isAdmin ? 'Ready. Choose Deliver.' : 'Ready. A steward can deliver it.' };
}

function guidanceErrorResult(error: unknown): GuidanceAcceptResult | null {
  if (!(error instanceof ApiError) || !error.payload || typeof error.payload !== 'object') {
    return null;
  }
  const payload = error.payload as Partial<GuidanceAcceptResult>;
  if (payload.status !== 'pending' || typeof payload.outcome !== 'string') return null;
  return payload as GuidanceAcceptResult;
}

function acceptanceFeedback(result: GuidanceAcceptResult): Feedback {
  if (isStrictGuidanceDelivery(result)) {
    return {
      state: 'DELIVERED',
      line: 'The assistant reads this note from your next question.',
      detail: `Accepted after graph and agent-store readback (version ${result.version}).`,
    };
  }
  return { state: 'PENDING', line: `${NOT_ACCEPTED} Choose Preview delivery and try again.` };
}

function failureFeedback(error: unknown, pending: GuidanceAcceptResult | null): Feedback {
  if (pending?.drift?.proposal) {
    // The server undid the delivery but could not prove the proposal's own
    // status went back to pending, so "nothing was accepted" may be untrue.
    return { state: 'UNAVAILABLE', line: PROPOSAL_NOT_RESTORED };
  }
  if (pending?.outcome === 'unavailable' || (error instanceof ApiError && error.status === 503)) {
    return { state: 'UNAVAILABLE', line: 'Delivery could not be checked right now, so nothing was accepted. Try again in a moment.' };
  }
  if (pending?.error === 'stale_preview') {
    return { state: 'WOULD EVICT', line: 'The guidance changed since your preview. Review the updated list of notes to remove.' };
  }
  if (pending?.outcome === 'would_evict') {
    return { state: 'WOULD EVICT', line: 'Confirm which older notes are removed before this note is delivered.' };
  }
  return { state: 'PENDING', line: `${NOT_ACCEPTED} Try again.` };
}

function useProposalPreview(
  mappingId: string,
  isAdmin: boolean,
  setPreview: (preview: GuidancePreview | null) => void,
  setFeedback: (feedback: Feedback) => void,
) {
  const query = useQuery(guidancePreviewQueryOptions(mappingId, false));
  const readPreview = async () => {
    const result = await query.refetch();
    if (result.error || !result.data) {
      setFeedback({ state: 'UNAVAILABLE', line: PREVIEW_UNREADABLE });
      return;
    }
    setPreview(result.data.preview);
    setFeedback(previewFeedback(result.data.preview, isAdmin));
  };
  return { isFetching: query.isFetching, readPreview };
}

/** Confirmation for a proven delivery. It goes to the shared toast queue, not
 * only the card: the accept mutation awaits the queue refetch, and the
 * refetched queue no longer holds the delivered note, so the card has usually
 * unmounted by the time `mutateAsync` resolves. */
function announceDelivered(systemLabel: string | null): void {
  pushToast('success', systemLabel
    ? tf('guidance.deliveredToast', { system: systemLabel })
    : t('guidance.deliveredToastNoSystem'));
}

function useProposalDelivery(
  mappingId: string,
  systemLabel: string | null,
  setPreview: (preview: GuidancePreview | null) => void,
  setFeedback: (feedback: Feedback) => void,
  setLossOpen: (open: boolean) => void,
) {
  const client = useQueryClient();
  const mutation = useMutation(acceptGuidanceMutationOptions(client));
  const deliver = async (confirmation?: GuidanceEvictionConfirmation) => {
    try {
      const result = await mutation.mutateAsync({ mappingId, confirmation });
      const next = acceptanceFeedback(result);
      setFeedback(next);
      if (next.state === 'DELIVERED') {
        setLossOpen(false);
        announceDelivered(systemLabel);
      }
    } catch (error) {
      const pending = guidanceErrorResult(error);
      if (pending?.preview) setPreview(pending.preview);
      const next = failureFeedback(error, pending);
      setFeedback(next);
      if (next.state === 'WOULD EVICT' && pending?.preview) setLossOpen(true);
    }
  };
  return { deliver, isPending: mutation.isPending };
}

function ProposalPreviewData({ preview }: { preview: GuidancePreview }) {
  return (
    <div className="guidance-preview-data" style={{ ...compactStyle, fontFamily: MONO, marginTop: '0.3rem', overflowWrap: 'anywhere' }}>
      outcome {preview.outcome}
      <br />current {numberOrUnavailable(preview.current_length)} chars · note {numberOrUnavailable(preview.note_length)} · result {numberOrUnavailable(preview.result_length)}
      <br />current digest {preview.current_digest || 'UNAVAILABLE'}
      {preview.evicted_note_digests.map((digest, index) => (
        <div key={`${digest}-${index}`}>evict[{index}] {digest}</div>
      ))}
    </div>
  );
}

/** Steward detail, collapsed by default: scope, the preview's lengths and
 * digests, and the delivered version. A collapsed <details> hides its body
 * from Playwright, so e2e must open the summary first. */
function ProposalTechnicalDetail({ scope, preview, detail }: {
  scope: string;
  preview: GuidancePreview | null;
  detail?: string;
}) {
  return (
    <details className="guidance-proposal-technical" style={{ marginTop: '0.35rem' }}>
      <summary style={{ ...compactStyle, cursor: 'pointer' }}>Technical detail</summary>
      <div className="guidance-proposal-scope" style={{ ...compactStyle, fontFamily: MONO, marginTop: '0.3rem' }}>scope {scope}</div>
      {preview && <ProposalPreviewData preview={preview} />}
      {detail && <div className="guidance-proposal-detail" style={{ ...compactStyle, fontFamily: MONO, marginTop: '0.3rem' }}>{detail}</div>}
    </details>
  );
}

interface ProposalActionsProps {
  preview: GuidancePreview | null;
  isAdmin: boolean;
  previewBusy: boolean;
  deliveryBusy: boolean;
  onPreview: () => void;
  onDeliver: () => void;
  onLoss: () => void;
}

function ProposalActions(props: ProposalActionsProps) {
  const { preview, isAdmin, previewBusy, deliveryBusy, onPreview, onDeliver, onLoss } = props;
  return (
    <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.45rem' }}>
      <button type="button" style={actionStyle} onClick={onPreview} disabled={previewBusy}>
        {previewBusy ? 'Reading preview…' : 'Preview delivery'}
      </button>
      {isAdmin && preview?.outcome === 'append' && (
        <button type="button" style={actionStyle} onClick={onDeliver} disabled={deliveryBusy}>Deliver</button>
      )}
      {isAdmin && preview?.outcome === 'would_evict' && (
        <button type="button" style={actionStyle} onClick={onLoss}>Review what is removed</button>
      )}
    </div>
  );
}

export function GuidanceProposalCard({ mapping, isAdmin }: { mapping: ProposedMapping; isAdmin: boolean }) {
  const [preview, setPreview] = useState<GuidancePreview | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(INITIAL_FEEDBACK);
  const [lossOpen, setLossOpen] = useState(false);
  const names = useSystemDisplayNames();
  const systemLabel = mapping.from_system ? systemDisplayLabel(mapping.from_system, names) : null;
  const previewFlow = useProposalPreview(mapping.mapping_id, isAdmin, setPreview, setFeedback);
  const delivery = useProposalDelivery(mapping.mapping_id, systemLabel, setPreview, setFeedback, setLossOpen);
  const confirmLoss = () => {
    if (!preview?.current_digest) return;
    void delivery.deliver({
      current_digest: preview.current_digest,
      evicted_note_digests: [...preview.evicted_note_digests],
    });
  };
  return (
    <article className="guidance-proposal" data-mapping-id={mapping.mapping_id} style={panelStyle}>
      <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <AuthorityTag state={feedback.state} label={CARD_LABEL[feedback.state]} />
        <strong style={{ fontSize: '0.78rem' }}>{systemLabel ?? 'Unknown system'}</strong>
      </div>
      <div style={{ fontSize: '0.8rem', margin: '0.4rem 0', whiteSpace: 'pre-wrap' }}>{mapping.to_value || 'No note text supplied.'}</div>
      <div className="guidance-proposal-line" role={feedback.state === 'UNAVAILABLE' ? 'alert' : 'status'} style={{ ...compactStyle, color: stateColor(feedback.state) }}>{feedback.line}</div>
      <ProposalActions preview={preview} isAdmin={isAdmin} previewBusy={previewFlow.isFetching} deliveryBusy={delivery.isPending} onPreview={() => void previewFlow.readPreview()} onDeliver={() => void delivery.deliver()} onLoss={() => setLossOpen(true)} />
      <ProposalTechnicalDetail scope={mapping.guidance_scope || 'legacy_unscoped'} preview={preview} detail={feedback.detail} />
      <GuidanceLossDialog preview={preview} open={lossOpen} busy={delivery.isPending} onCancel={() => setLossOpen(false)} onConfirm={confirmLoss} />
    </article>
  );
}
