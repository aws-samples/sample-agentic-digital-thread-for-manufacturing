// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ConfirmHost/> — renders the head askConfirm() request (confirm-store.ts)
 * through the shared <ConfirmDialog/>. Mounted once per page, next to
 * <ToastRegion/> (main-admin.tsx, pages/app/AppShell.tsx).
 *
 * Each request mounts its own dialog (keyed by request id), so ModalSurface
 * captures the element that was focused when THAT request opened and returns
 * focus to it on close, and a queued request opens fresh after the previous
 * one closes. A second mounted host stays idle (only the primary renders).
 * A blank line in the body starts a new paragraph; the text renders as
 * literal text nodes (repo sink policy: no HTML strings). */
import { useEffect, useState } from 'react';

import { useStore } from '../../lib/store';
import { ConfirmDialog } from './ConfirmDialog';
import {
  allocateHostId,
  confirmStore,
  registerConfirmHost,
  settleConfirm,
  type ConfirmRequest,
} from './confirm-store';

export function ConfirmHost() {
  const [hostId] = useState(allocateHostId);
  useEffect(() => registerConfirmHost(hostId), [hostId]);
  const head = useStore(confirmStore, (state): ConfirmRequest | null =>
    state.primaryHost === hostId ? state.queue[0] ?? null : null,
  );

  if (!head) return null;
  return (
    <ConfirmDialog
      key={head.id}
      open
      title={head.title}
      confirmLabel={head.confirmLabel}
      cancelLabel={head.cancelLabel}
      tone={head.tone}
      onConfirm={() => settleConfirm(head.id, true)}
      onCancel={() => settleConfirm(head.id, false)}
    >
      {head.body.split(/\n{2,}/).map((paragraph, index) => (
        <p key={index}>{paragraph}</p>
      ))}
    </ConfirmDialog>
  );
}

export default ConfirmHost;
