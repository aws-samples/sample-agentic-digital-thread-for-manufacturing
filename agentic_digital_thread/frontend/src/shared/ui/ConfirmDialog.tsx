// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ConfirmDialog/> — a small confirmation modal for consequential actions.
 *
 * Built on ModalSurface (focus trap, Escape/overlay close, a11y) so every
 * confirm in the app behaves identically. The IMPACT of the action belongs in
 * the body here — not crammed into the triggering button's label (review
 * feedback 2026-08-03: "Deactivate — remove from authority resolution" was
 * wordy AND still didn't explain the consequence). Buttons: a plain Cancel and
 * a styled confirm; confirm is the LAST element so keyboard order reads
 * body → cancel → confirm.
 *
 * Presentation follows the other ModalSurface consumers: the surface root is
 * the full-viewport backdrop (`.confirm-dialog-overlay`, base.css — loaded by
 * both the app and admin entries) and the centred card is `.confirm-dialog`.
 * Clicking the backdrop cancels, as it does for every other app modal. The
 * visible <h3> labels the dialog, so the title is not duplicated in the DOM.
 *
 * Double-click guard: a dialog opened by the FIRST click of a double-click
 * renders before the second click lands, and that second click hits the
 * backdrop. A backdrop click with a click count above 1 (event.detail) is
 * therefore ignored; a deliberate single click still cancels. */
import { useId, type MouseEvent, type ReactNode } from 'react';

import { ModalSurface } from './ModalSurface';

export type ConfirmDialogProps = {
  open: boolean;
  title: string;
  /** Impact statement: what happens, to whom, and whether it is reversible. */
  children: ReactNode;
  confirmLabel: string;
  /** Label of the dismiss button (default "Cancel"; translated in the chat). */
  cancelLabel?: string;
  /** 'danger' colors the confirm button for destructive/consequential acts. */
  tone?: 'danger' | 'default';
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  cancelLabel = 'Cancel',
  tone = 'default',
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const titleId = `confirm-dialog-title-${useId().replace(/:/g, '')}`;
  // The second click of the double-click that opened the dialog lands on the
  // backdrop: preventDefault tells ModalSurface not to treat it as a cancel.
  const ignoreRepeatBackdropClick = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget && event.detail > 1) event.preventDefault();
  };
  return (
    <ModalSurface
      open={open}
      onClose={onCancel}
      labelledBy={titleId}
      closeOnOverlayClick
      className="confirm-dialog-overlay"
      onClick={ignoreRepeatBackdropClick}
    >
      <div className="confirm-dialog">
        <h3 id={titleId} className="confirm-dialog-title">
          {title}
        </h3>
        <div className="confirm-dialog-body">{children}</div>
        <div className="confirm-dialog-actions">
          <button type="button" className="confirm-dialog-btn confirm-dialog-cancel" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={
              tone === 'danger'
                ? 'confirm-dialog-btn confirm-danger-btn'
                : 'confirm-dialog-btn btn-primary'
            }
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </ModalSurface>
  );
}

export default ConfirmDialog;
