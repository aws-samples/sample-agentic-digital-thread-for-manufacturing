// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useRef, useState } from 'react';

import type { GuidancePreview } from '../../../../api/types/proposals';
import { ModalSurface } from '../../../../shared/ui/ModalSurface';
import { AuthorityTag, compactStyle, MONO, RED } from './guidance-stability-ui';

interface LossDialogProps {
  preview: GuidancePreview | null;
  open: boolean;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

function LossDigestList({ preview }: { preview: GuidancePreview }) {
  return (
    <>
      <div style={{ ...compactStyle, fontFamily: MONO, overflowWrap: 'anywhere' }}>
        current guidance: {preview.current_digest}
      </div>
      <ol aria-label="Ordered evicted guidance digests" style={{ ...compactStyle, fontFamily: MONO, paddingLeft: '1.25rem', overflowWrap: 'anywhere' }}>
        {preview.evicted_note_digests.map((digest, index) => (
          <li key={`${digest}-${index}`}>{digest}</li>
        ))}
      </ol>
    </>
  );
}

function noteCount(count: number): string {
  return `${count} older learned note${count === 1 ? '' : 's'}`;
}

function LossActions({ checked, busy, onCancel, onConfirm }: {
  checked: boolean;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginTop: '0.8rem' }}>
      <button type="button" onClick={onCancel}>Cancel</button>
      <button
        type="button"
        className="confirm-danger-btn"
        disabled={!checked || busy}
        onClick={onConfirm}
        style={{ background: RED, color: '#fff', borderRadius: 4 }}
      >
        {busy ? 'Delivering…' : 'Remove them and deliver'}
      </button>
    </div>
  );
}

export function GuidanceLossDialog(props: LossDialogProps) {
  const { preview, open, busy, onCancel, onConfirm } = props;
  const [checked, setChecked] = useState(false);
  const checkboxRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (open) setChecked(false);
  }, [open, preview?.current_digest]);
  if (!preview) return null;
  const removed = noteCount(preview.evicted_note_digests.length);
  return (
    <ModalSurface open={open} onClose={onCancel} label="Confirm removal of older notes" initialFocus={checkboxRef} style={{ maxWidth: 560, padding: '1rem 1.15rem' }}>
      <h3 style={{ margin: '0 0 0.4rem', fontSize: '0.95rem' }}>
        <AuthorityTag state="WOULD EVICT" label="Confirm removal" /> Remove older notes to make room?
      </h3>
      <p style={{ fontSize: '0.78rem', lineHeight: 1.45 }}>
        Delivering this note removes the {removed} listed below from this system's guidance, oldest first. They are listed by fingerprint. This confirmation covers the guidance as it is now: if it changes before you deliver, you are asked again.
      </p>
      <LossDigestList preview={preview} />
      <label style={{ display: 'flex', alignItems: 'flex-start', gap: '0.4rem', fontSize: '0.78rem' }}>
        <input ref={checkboxRef} type="checkbox" checked={checked} onChange={(event) => setChecked(event.target.checked)} />
        I understand that the {removed} listed above will be removed.
      </label>
      <LossActions checked={checked} busy={busy} onCancel={onCancel} onConfirm={onConfirm} />
    </ModalSurface>
  );
}
